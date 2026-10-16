#include "reservation_manager.h"

#include "cgroup_manager.h"

#include <algorithm>
#include <limits>
#include <stdexcept>
#include <utility>

namespace {
bool addWouldOverflow(std::uint64_t left, std::uint64_t right) noexcept {
  return right > std::numeric_limits<std::uint64_t>::max() - left;
}

bool sumExceeds(std::uint64_t left, std::uint64_t right,
                std::uint64_t limit) noexcept {
  return left > limit || right > limit - left;
}

} // namespace

ReservationManager::ReservationManager(ReservationManagerConfig config,
                                       LimitWriter limit_writer)
    : config_(config), limit_writer_(std::move(limit_writer)) {
  if (config_.parent_limit_bytes == 0) {
    throw std::invalid_argument("parent limit must be positive");
  }
  if (sumExceeds(config_.reserved_memory_capacity_bytes,
                 config_.checkpoint_budget_bytes, config_.parent_limit_bytes)) {
    throw std::invalid_argument(
        "Reserved Memory and checkpoint budget exceed parent limit");
  }
  if (!limit_writer_) {
    throw std::invalid_argument("reservation manager requires a limit writer");
  }
}

void ReservationManager::registerJob(const std::string &job_id,
                                     std::uint64_t base_quota_bytes) {
  if (!CgroupV2Manager::validJobId(job_id)) {
    throw std::invalid_argument("invalid job id: " + job_id);
  }
  if (base_quota_bytes == 0) {
    throw std::invalid_argument("base quota must be positive");
  }

  std::lock_guard lock(mutex_);
  if (const auto existing = jobs_.find(job_id); existing != jobs_.end()) {
    if (existing->second.base_quota_bytes == base_quota_bytes) {
      return;
    }
    throw std::runtime_error(
        "job already registered with a different base quota: " + job_id);
  }

  const auto reserved_capacity =
      config_.reserved_memory_capacity_bytes + config_.checkpoint_budget_bytes;
  const auto base_capacity = config_.parent_limit_bytes - reserved_capacity;
  if (sumExceeds(base_committed_bytes_, base_quota_bytes, base_capacity)) {
    throw std::runtime_error("insufficient parent capacity for job base quota");
  }

  jobs_.emplace(job_id, JobAccount{.job_id = job_id,
                                   .base_quota_bytes = base_quota_bytes,
                                   .borrowed_bytes = 0});
  base_committed_bytes_ += base_quota_bytes;
}

void ReservationManager::unregisterJob(const std::string &job_id) {
  std::lock_guard lock(mutex_);
  const auto job = jobs_.find(job_id);
  if (job == jobs_.end()) {
    throw std::runtime_error("job is not registered: " + job_id);
  }
  if (job->second.borrowed_bytes != 0) {
    throw std::runtime_error("job still has outstanding reservations: " +
                             job_id);
  }
  if (job->second.checkpoint_bytes != 0) {
    throw std::runtime_error("job still has an outstanding checkpoint: " +
                             job_id);
  }
  erasePendingForJobLocked(job_id);
  base_committed_bytes_ -= job->second.base_quota_bytes;
  jobs_.erase(job);
}

CheckpointResult ReservationManager::beginCheckpoint(
    const std::string &job_id, const std::string &request_id,
    std::uint64_t memory_current_bytes,
    std::uint64_t host_mem_available_bytes,
    std::uint64_t safety_margin_bytes) {
  std::lock_guard lock(mutex_);
  const auto job = jobs_.find(job_id);
  if (job == jobs_.end()) {
    return {.decision = ReserveDecision::Denied,
            .reservation = std::nullopt,
            .effective_limit_bytes = 0,
            .available_bytes = config_.checkpoint_budget_bytes -
                               checkpoint_committed_bytes_,
            .reason = "job_not_registered"};
  }
  const auto available = config_.checkpoint_budget_bytes -
                         checkpoint_committed_bytes_;
  const auto current_limit = job->second.physicalLimitBytes();
  auto result = [&](ReserveDecision decision, std::string reason,
                    std::optional<CheckpointReservation> reservation =
                        std::nullopt) {
    return CheckpointResult{.decision = decision,
                            .reservation = std::move(reservation),
                            .effective_limit_bytes =
                                job->second.physicalLimitBytes(),
                            .available_bytes = config_.checkpoint_budget_bytes -
                                               checkpoint_committed_bytes_,
                            .reason = std::move(reason)};
  };
  if (request_id.empty()) {
    return result(ReserveDecision::Denied, "request_id_empty");
  }
  if (config_.checkpoint_budget_bytes == 0) {
    return result(ReserveDecision::Denied, "checkpoint_budget_zero");
  }
  const auto key = std::pair{job_id, request_id};
  if (const auto old = checkpoint_requests_.find(key);
      old != checkpoint_requests_.end()) {
    const auto &record = checkpoints_.at(old->second);
    if (record.state == ReservationState::Active) {
      return result(ReserveDecision::Granted, "duplicate_request", record);
    }
    if (record.state == ReservationState::Released) {
      return result(ReserveDecision::Denied, "request_already_released", record);
    }
    return result(ReserveDecision::Denied, "request_already_ended", record);
  }
  if (addWouldOverflow(config_.checkpoint_budget_bytes, safety_margin_bytes) ||
      sumExceeds(config_.checkpoint_budget_bytes + safety_margin_bytes,
                 config_.host_headroom_bytes, host_mem_available_bytes)) {
    return result(ReserveDecision::Wait, "host_headroom");
  }
  for (auto &[token, record] : checkpoints_) {
    (void)token;
    if (record.job_id != job_id || record.state == ReservationState::Released) {
      continue;
    }
    if (record.state == ReservationState::Active) {
      return result(ReserveDecision::Wait, "checkpoint_already_active");
    }
    // A previous save finished, but its pages still keep the cgroup above
    // the base+batch limit. Transfer its capacity to a fresh token so a
    // delayed duplicate END for the old save cannot release the new save.
    CheckpointReservation replacement{
        .token = "ckpt-" + std::to_string(next_token_id_++),
        .request_id = request_id,
        .job_id = job_id,
        .bytes = record.bytes,
        .state = ReservationState::Active};
    checkpoints_.emplace(replacement.token, replacement);
    try {
      checkpoint_requests_.emplace(key, replacement.token);
    } catch (...) {
      checkpoints_.erase(replacement.token);
      throw;
    }
    record.state = ReservationState::Released;
    return result(ReserveDecision::Granted, "transferred_pending_checkpoint",
                  replacement);
  }
  if (available < config_.checkpoint_budget_bytes) {
    return result(ReserveDecision::Wait, "checkpoint_budget_busy");
  }
  if (addWouldOverflow(current_limit, config_.checkpoint_budget_bytes) ||
      current_limit + config_.checkpoint_budget_bytes >
          config_.parent_limit_bytes) {
    return result(ReserveDecision::Wait, "parent_limit");
  }
  (void)memory_current_bytes;
  CheckpointReservation record{
      .token = "ckpt-" + std::to_string(next_token_id_++),
      .request_id = request_id,
      .job_id = job_id,
      .bytes = config_.checkpoint_budget_bytes,
      .state = ReservationState::Active};
  checkpoints_.emplace(record.token, record);
  try {
    checkpoint_requests_.emplace(key, record.token);
    limit_writer_(job_id, current_limit + record.bytes);
  } catch (...) {
    checkpoint_requests_.erase(key);
    checkpoints_.erase(record.token);
    throw;
  }
  job->second.checkpoint_bytes = record.bytes;
  checkpoint_committed_bytes_ += record.bytes;
  return result(ReserveDecision::Granted, "granted", record);
}

CheckpointReleaseResult ReservationManager::endCheckpoint(
    const std::string &job_id, const std::string &token,
    std::uint64_t memory_current_bytes,
    std::uint64_t safety_margin_bytes) {
  std::lock_guard lock(mutex_);
  const auto job = jobs_.find(job_id);
  if (job == jobs_.end()) {
    return {.reason = "job_not_registered"};
  }
  auto result = [&](ReleaseDecision decision, std::string reason) {
    return CheckpointReleaseResult{
        .decision = decision,
        .effective_limit_bytes = job->second.physicalLimitBytes(),
        .available_bytes = config_.checkpoint_budget_bytes -
                           checkpoint_committed_bytes_,
        .reason = std::move(reason)};
  };
  const auto found = checkpoints_.find(token);
  if (found == checkpoints_.end()) {
    return result(ReleaseDecision::Denied, "token_not_found");
  }
  auto &record = found->second;
  if (record.job_id != job_id) {
    return result(ReleaseDecision::Denied, "token_owner_mismatch");
  }
  if (record.state == ReservationState::Released) {
    return result(ReleaseDecision::AlreadyReleased, "already_released");
  }
  const auto target = job->second.effectiveLimitBytes();
  std::uint64_t inflight_estimated = 0;
  for (const auto &[batch_token, batch] : reservations_) {
    (void)batch_token;
    if (batch.job_id != job_id || batch.state != ReservationState::Active) {
      continue;
    }
    if (addWouldOverflow(inflight_estimated,
                         batch.remaining_estimated_bytes)) {
      record.state = ReservationState::PendingRelease;
      return result(ReleaseDecision::Pending, "inflight_estimate_overflow");
    }
    inflight_estimated += batch.remaining_estimated_bytes;
  }
  if (addWouldOverflow(memory_current_bytes, inflight_estimated) ||
      addWouldOverflow(memory_current_bytes + inflight_estimated,
                       safety_margin_bytes) ||
      memory_current_bytes + inflight_estimated + safety_margin_bytes > target) {
    record.state = ReservationState::PendingRelease;
    return result(ReleaseDecision::Pending, "memory_usage_above_proposed_limit");
  }
  limit_writer_(job_id, target);
  job->second.checkpoint_bytes = 0;
  checkpoint_committed_bytes_ -= record.bytes;
  record.state = ReservationState::Released;
  return result(ReleaseDecision::Released, "released");
}

ReserveResult ReservationManager::reserve(
    const std::string &job_id, const std::string &request_id,
    std::uint64_t estimated_allocation_bytes,
    std::uint64_t memory_current_bytes, std::uint64_t host_mem_available_bytes,
    std::uint64_t inactive_file_bytes, std::uint64_t safety_margin_bytes) {
  std::lock_guard lock(mutex_);
  const auto job = jobs_.find(job_id);
  if (job == jobs_.end()) {
    return {.decision = ReserveDecision::Denied,
            .reservation = std::nullopt,
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "job_not_registered"};
  }
  if (request_id.empty()) {
    return {.decision = ReserveDecision::Denied,
            .reservation = std::nullopt,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "request_id_empty"};
  }
  if (estimated_allocation_bytes == 0) {
    return {.decision = ReserveDecision::Denied,
            .reservation = std::nullopt,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "estimated_allocation_zero"};
  }

  // A client waiting inside RESERVE cannot issue a separate RETRY command.
  // Re-evaluate this job's pending releases on every reserve attempt so quota
  // that became safe to return cannot permanently block the FIFO head.
  for (auto &[token, outstanding] : reservations_) {
    (void)token;
    if (outstanding.job_id == job_id &&
        outstanding.state == ReservationState::PendingRelease) {
      (void)releaseLocked(job_id, outstanding, memory_current_bytes,
                          safety_margin_bytes);
    }
  }

  const auto request_key = std::pair{job_id, request_id};
  if (const auto duplicate = request_tokens_.find(request_key);
      duplicate != request_tokens_.end()) {
    const auto &reservation = reservations_.at(duplicate->second);
    if (reservation.job_id != job_id ||
        reservation.estimated_bytes != estimated_allocation_bytes) {
      return {.decision = ReserveDecision::Denied,
              .reservation = reservation,
              .effective_limit_bytes = job->second.effectiveLimitBytes(),
              .borrowed_bytes = reservation.bytes,
              .reserved_memory_available_bytes =
                  reservedMemoryAvailableLocked(),
              .reason = "request_id_conflict"};
    }
    if (reservation.state == ReservationState::Released) {
      return {.decision = ReserveDecision::Denied,
              .reservation = reservation,
              .effective_limit_bytes = job->second.effectiveLimitBytes(),
              .borrowed_bytes = reservation.bytes,
              .reserved_memory_available_bytes =
                  reservedMemoryAvailableLocked(),
              .reason = "request_already_released"};
    }
    return {.decision = ReserveDecision::Granted,
            .reservation = reservation,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .borrowed_bytes = reservation.bytes,
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "duplicate_request"};
  }

  // Even at memory.current == 0, a job can use at most its base quota plus the
  // entire Reserved Memory pool. Do not enqueue a request that can never fit.
  if (addWouldOverflow(estimated_allocation_bytes, safety_margin_bytes) ||
      addWouldOverflow(job->second.base_quota_bytes,
                       config_.reserved_memory_capacity_bytes) ||
      estimated_allocation_bytes + safety_margin_bytes >
          job->second.base_quota_bytes +
              config_.reserved_memory_capacity_bytes) {
    erasePendingLocked(request_key);
    return {.decision = ReserveDecision::Denied,
            .reservation = std::nullopt,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "estimated_allocation_exceeds_job_capacity"};
  }

  const auto pending = pending_requests_.find(request_key);
  if (pending != pending_requests_.end() &&
      pending->second.estimated_bytes != estimated_allocation_bytes) {
    return {.decision = ReserveDecision::Denied,
            .reservation = std::nullopt,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "request_id_conflict"};
  }

  // inactive_file is the conservative kernel signal for filesystem-backed
  // pages not currently on the active list. They remain inside memory.current
  // and under the hard memory.max boundary, but need not consume Reserved
  // Memory as if they were anonymous working-set memory.
  const auto reclaimable_file =
      std::min(memory_current_bytes, inactive_file_bytes);
  const auto protected_current = memory_current_bytes - reclaimable_file;
  std::uint64_t job_inflight_estimated = 0;
  std::uint64_t global_inflight_estimated = 0;
  for (const auto &[token, active] : reservations_) {
    (void)token;
    if (active.state != ReservationState::Active) {
      continue;
    }
    if (addWouldOverflow(global_inflight_estimated,
                         active.remaining_estimated_bytes)) {
      erasePendingLocked(request_key);
      return {.decision = ReserveDecision::Denied,
              .reservation = std::nullopt,
              .effective_limit_bytes = job->second.effectiveLimitBytes(),
              .reserved_memory_available_bytes =
                  reservedMemoryAvailableLocked(),
              .reason = "global_inflight_estimate_overflow"};
    }
    global_inflight_estimated += active.remaining_estimated_bytes;
    if (active.job_id == job_id) {
      if (addWouldOverflow(job_inflight_estimated,
                           active.remaining_estimated_bytes)) {
        erasePendingLocked(request_key);
        return {.decision = ReserveDecision::Denied,
                .reservation = std::nullopt,
                .effective_limit_bytes = job->second.effectiveLimitBytes(),
                .reserved_memory_available_bytes =
                    reservedMemoryAvailableLocked(),
                .reason = "job_inflight_estimate_overflow"};
      }
      job_inflight_estimated += active.remaining_estimated_bytes;
    }
  }
  if (addWouldOverflow(protected_current, job_inflight_estimated) ||
      addWouldOverflow(protected_current + job_inflight_estimated,
                       estimated_allocation_bytes) ||
      addWouldOverflow(protected_current + job_inflight_estimated +
                           estimated_allocation_bytes,
                       safety_margin_bytes)) {
    erasePendingLocked(request_key);
    return {.decision = ReserveDecision::Denied,
            .reservation = std::nullopt,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "projected_peak_overflow"};
  }
  const auto effective_limit = job->second.effectiveLimitBytes();
  const auto projected_peak = protected_current + job_inflight_estimated +
                              estimated_allocation_bytes + safety_margin_bytes;
  const auto borrow_needed =
      projected_peak > effective_limit ? projected_peak - effective_limit : 0;

  // A new request may not bypass an older waiter, even if the new request is
  // smaller and happens to fit at this instant. The client at the FIFO head
  // receives the next opportunity to reserve capacity.
  if (pending == pending_requests_.end() && !pending_order_.empty()) {
    enqueuePendingLocked(request_key, estimated_allocation_bytes);
    return {.decision = ReserveDecision::Wait,
            .reservation = std::nullopt,
            .effective_limit_bytes = effective_limit,
            .borrowed_bytes = borrow_needed,
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "queued_behind_earlier_request"};
  }
  if (pending != pending_requests_.end() &&
      pending_order_.front() != request_key) {
    return {.decision = ReserveDecision::Wait,
            .reservation = std::nullopt,
            .effective_limit_bytes = effective_limit,
            .borrowed_bytes = borrow_needed,
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "queued_behind_earlier_request"};
  }

  if (borrow_needed > reservedMemoryAvailableLocked()) {
    if (pending == pending_requests_.end()) {
      enqueuePendingLocked(request_key, estimated_allocation_bytes);
    }
    return {.decision = ReserveDecision::Wait,
            .reservation = std::nullopt,
            .effective_limit_bytes = effective_limit,
            .borrowed_bytes = borrow_needed,
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "reserved_memory_exhausted"};
  }
  // Host pressure is about how much memory the workload is about to allocate,
  // not how much cgroup quota must be added. Never substitute borrow_needed
  // for the full estimate in this safety check.
  if (addWouldOverflow(global_inflight_estimated,
                       estimated_allocation_bytes) ||
      addWouldOverflow(global_inflight_estimated + estimated_allocation_bytes,
                       safety_margin_bytes) ||
      sumExceeds(global_inflight_estimated + estimated_allocation_bytes +
                     safety_margin_bytes,
                 config_.host_headroom_bytes, host_mem_available_bytes)) {
    if (pending == pending_requests_.end()) {
      enqueuePendingLocked(request_key, estimated_allocation_bytes);
    }
    return {.decision = ReserveDecision::Wait,
            .reservation = std::nullopt,
            .effective_limit_bytes = effective_limit,
            .borrowed_bytes = borrow_needed,
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "host_headroom"};
  }
  if (addWouldOverflow(effective_limit, borrow_needed)) {
    return {.decision = ReserveDecision::Denied,
            .reservation = std::nullopt,
            .effective_limit_bytes = effective_limit,
            .borrowed_bytes = borrow_needed,
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "effective_limit_overflow"};
  }

  const auto new_limit = effective_limit + borrow_needed;
  if (addWouldOverflow(new_limit, job->second.checkpoint_bytes) ||
      new_limit + job->second.checkpoint_bytes > config_.parent_limit_bytes) {
    if (pending == pending_requests_.end()) {
      enqueuePendingLocked(request_key, estimated_allocation_bytes);
    }
    return {.decision = ReserveDecision::Wait,
            .reservation = std::nullopt,
            .effective_limit_bytes = effective_limit,
            .borrowed_bytes = borrow_needed,
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "projected_peak_exceeds_parent"};
  }

  ReservationRecord reservation{
      .token = nextTokenLocked(),
      .request_id = request_id,
      .job_id = job_id,
      .estimated_bytes = estimated_allocation_bytes,
      .remaining_estimated_bytes = estimated_allocation_bytes,
      .bytes = borrow_needed,
      .state = ReservationState::Active,
  };
  // Allocate all fallible in-memory records before changing the kernel limit.
  // They remain invisible to other calls because the accounting mutex is held.
  reservations_.emplace(reservation.token, reservation);
  try {
    request_tokens_.emplace(request_key, reservation.token);
  } catch (...) {
    reservations_.erase(reservation.token);
    throw;
  }

  // Keep the accounting mutex while updating the cgroup. This serializes
  // concurrent grants. The caller must not receive GRANTED until this write
  // has succeeded.
  if (borrow_needed != 0) {
    try {
      limit_writer_(job_id, new_limit + job->second.checkpoint_bytes);
    } catch (...) {
      request_tokens_.erase(request_key);
      reservations_.erase(reservation.token);
      throw;
    }
  }

  job->second.borrowed_bytes += borrow_needed;
  reserved_memory_committed_bytes_ += borrow_needed;
  erasePendingLocked(request_key);

  return {.decision = ReserveDecision::Granted,
          .reservation = reservation,
          .effective_limit_bytes = new_limit,
          .borrowed_bytes = borrow_needed,
          .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
          .reason = "granted"};
}

bool ReservationManager::cancelPending(const std::string &job_id,
                                       const std::string &request_id) {
  std::lock_guard lock(mutex_);
  const auto key = std::pair{job_id, request_id};
  if (!pending_requests_.contains(key)) {
    return false;
  }
  erasePendingLocked(key);
  return true;
}

ShrinkResult ReservationManager::shrink(
    const std::string &job_id, const std::string &token,
    std::uint64_t remaining_allocation_bytes,
    std::uint64_t memory_current_bytes, std::uint64_t inactive_file_bytes,
    std::uint64_t safety_margin_bytes) {
  std::lock_guard lock(mutex_);
  const auto job = jobs_.find(job_id);
  if (job == jobs_.end()) {
    return {.decision = ShrinkDecision::Denied,
            .reservation = std::nullopt,
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "job_not_registered"};
  }
  const auto reservation = reservations_.find(token);
  if (reservation == reservations_.end()) {
    return {.decision = ShrinkDecision::Denied,
            .reservation = std::nullopt,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "token_not_found"};
  }
  auto &record = reservation->second;
  if (record.job_id != job_id) {
    return {.decision = ShrinkDecision::Denied,
            .reservation = record,
            .previous_borrowed_bytes = record.bytes,
            .borrowed_bytes = record.bytes,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "token_owner_mismatch"};
  }
  if (record.state == ReservationState::Released) {
    return {.decision = ShrinkDecision::AlreadyReleased,
            .reservation = record,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "already_released"};
  }
  if (record.state == ReservationState::PendingRelease) {
    return {.decision = ShrinkDecision::Unchanged,
            .reservation = record,
            .previous_borrowed_bytes = record.bytes,
            .borrowed_bytes = record.bytes,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "release_already_pending"};
  }
  if (remaining_allocation_bytes > record.remaining_estimated_bytes) {
    return {.decision = ShrinkDecision::Denied,
            .reservation = record,
            .previous_borrowed_bytes = record.bytes,
            .borrowed_bytes = record.bytes,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "remaining_estimate_increase"};
  }
  const auto previous_remaining_estimate = record.remaining_estimated_bytes;
  record.remaining_estimated_bytes = remaining_allocation_bytes;
  if (record.bytes == 0) {
    return {.decision = ShrinkDecision::Unchanged,
            .reservation = record,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = previous_remaining_estimate == remaining_allocation_bytes
                          ? "no_borrow_to_shrink"
                          : "estimate_shrunk_without_borrow"};
  }
  if (record.bytes > job->second.borrowed_bytes) {
    throw std::logic_error("reservation accounting underflow");
  }

  const auto previous_bytes = record.bytes;
  const auto other_borrowed = job->second.borrowed_bytes - previous_bytes;
  const auto limit_without_reservation =
      job->second.base_quota_bytes + other_borrowed;
  const auto reclaimable_file =
      std::min(memory_current_bytes, inactive_file_bytes);
  const auto protected_current = memory_current_bytes - reclaimable_file;
  std::uint64_t other_inflight_estimated = 0;
  for (const auto &[other_token, active] : reservations_) {
    if (other_token == token || active.job_id != job_id ||
        active.state != ReservationState::Active) {
      continue;
    }
    if (addWouldOverflow(other_inflight_estimated,
                         active.remaining_estimated_bytes)) {
      record.remaining_estimated_bytes = previous_remaining_estimate;
      return {.decision = ShrinkDecision::Denied,
              .reservation = record,
              .previous_borrowed_bytes = previous_bytes,
              .borrowed_bytes = previous_bytes,
              .effective_limit_bytes = job->second.effectiveLimitBytes(),
              .reserved_memory_available_bytes =
                  reservedMemoryAvailableLocked(),
              .reason = "job_inflight_estimate_overflow"};
    }
    other_inflight_estimated += active.remaining_estimated_bytes;
  }
  if (addWouldOverflow(protected_current, other_inflight_estimated) ||
      addWouldOverflow(protected_current + other_inflight_estimated,
                       remaining_allocation_bytes)) {
    record.remaining_estimated_bytes = previous_remaining_estimate;
    return {.decision = ShrinkDecision::Denied,
            .reservation = record,
            .previous_borrowed_bytes = previous_bytes,
            .borrowed_bytes = previous_bytes,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "projected_remaining_overflow"};
  }

  // Recompute how much total borrowed capacity the remaining workload phase
  // would need. Capacity already owned by other active tokens is kept intact;
  // only this token may be reduced by SHRINK.
  const auto projected_remaining = protected_current +
                                   other_inflight_estimated +
                                   remaining_allocation_bytes;
  const auto required_total_borrowed =
      projected_remaining > job->second.base_quota_bytes
          ? projected_remaining - job->second.base_quota_bytes
          : 0;
  const auto target_token_bytes =
      required_total_borrowed > other_borrowed
          ? required_total_borrowed - other_borrowed
          : 0;

  // The workload estimate may permit a large reduction while the allocator
  // still retains pages. Lower only as far as the observed cgroup usage plus
  // the release safety margin permits. This can produce a safe partial shrink
  // instead of an all-or-nothing pending release.
  if (addWouldOverflow(memory_current_bytes, safety_margin_bytes)) {
    return {.decision = ShrinkDecision::Unchanged,
            .reservation = record,
            .previous_borrowed_bytes = previous_bytes,
            .borrowed_bytes = previous_bytes,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "memory_usage_overflow"};
  }
  const auto safe_limit = memory_current_bytes + safety_margin_bytes;
  const auto safe_token_bytes =
      safe_limit > limit_without_reservation
          ? safe_limit - limit_without_reservation
          : 0;
  const auto retained_bytes =
      std::min(previous_bytes, std::max(target_token_bytes, safe_token_bytes));
  if (retained_bytes >= previous_bytes) {
    return {.decision = ShrinkDecision::Unchanged,
            .reservation = record,
            .previous_borrowed_bytes = previous_bytes,
            .borrowed_bytes = previous_bytes,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = target_token_bytes >= previous_bytes
                          ? "remaining_estimate_requires_current_capacity"
                          : "memory_usage_above_shrink_target"};
  }

  const auto returned_bytes = previous_bytes - retained_bytes;
  const auto proposed_limit =
      job->second.effectiveLimitBytes() - returned_bytes;
  limit_writer_(job_id, proposed_limit + job->second.checkpoint_bytes);
  job->second.borrowed_bytes -= returned_bytes;
  reserved_memory_committed_bytes_ -= returned_bytes;
  record.bytes = retained_bytes;
  return {.decision = ShrinkDecision::Shrunk,
          .reservation = record,
          .previous_borrowed_bytes = previous_bytes,
          .borrowed_bytes = retained_bytes,
          .returned_bytes = returned_bytes,
          .effective_limit_bytes = proposed_limit,
          .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
          .reason = retained_bytes == target_token_bytes
                        ? "shrunk_to_remaining_estimate"
                        : "partially_shrunk_to_safe_limit"};
}

ReleaseResult ReservationManager::release(const std::string &job_id,
                                          const std::string &token,
                                          std::uint64_t memory_current_bytes,
                                          std::uint64_t safety_margin_bytes) {
  std::lock_guard lock(mutex_);
  const auto reservation = reservations_.find(token);
  if (reservation == reservations_.end()) {
    return {.decision = ReleaseDecision::Denied,
            .reservation = std::nullopt,
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "token_not_found"};
  }
  return releaseLocked(job_id, reservation->second, memory_current_bytes,
                       safety_margin_bytes);
}

std::vector<ReleaseResult>
ReservationManager::retryPendingReleases(const std::string &job_id,
                                         std::uint64_t memory_current_bytes,
                                         std::uint64_t safety_margin_bytes) {
  std::lock_guard lock(mutex_);
  std::vector<ReleaseResult> results;
  for (auto &[token, reservation] : reservations_) {
    (void)token;
    if (reservation.job_id == job_id &&
        reservation.state == ReservationState::PendingRelease) {
      results.push_back(releaseLocked(job_id, reservation, memory_current_bytes,
                                      safety_margin_bytes));
    }
  }
  return results;
}

void ReservationManager::reclaimEmptyJob(const std::string &job_id) {
  std::lock_guard lock(mutex_);
  const auto job = jobs_.find(job_id);
  if (job == jobs_.end()) {
    erasePendingForJobLocked(job_id);
    return;
  }

  erasePendingForJobLocked(job_id);

  for (auto &[token, reservation] : reservations_) {
    (void)token;
    if (reservation.job_id == job_id &&
        reservation.state != ReservationState::Released) {
      reserved_memory_committed_bytes_ -= reservation.bytes;
      reservation.state = ReservationState::Released;
    }
  }
  for (auto &[token, checkpoint] : checkpoints_) {
    (void)token;
    if (checkpoint.job_id == job_id &&
        checkpoint.state != ReservationState::Released) {
      checkpoint_committed_bytes_ -= checkpoint.bytes;
      checkpoint.state = ReservationState::Released;
    }
  }
  base_committed_bytes_ -= job->second.base_quota_bytes;
  jobs_.erase(job);
}

ReservationManagerSnapshot ReservationManager::snapshot() const {
  std::lock_guard lock(mutex_);
  ReservationManagerSnapshot result{
      .config = config_,
      .base_committed_bytes = base_committed_bytes_,
      .reserved_memory_committed_bytes = reserved_memory_committed_bytes_,
      .checkpoint_committed_bytes = checkpoint_committed_bytes_,
      .jobs = jobs_,
      .reservations = reservations_,
      .checkpoints = checkpoints_,
      .pending_reservations = {}};
  result.pending_reservations.reserve(pending_order_.size());
  for (const auto &key : pending_order_) {
    result.pending_reservations.push_back(pending_requests_.at(key));
  }
  return result;
}

std::uint64_t
ReservationManager::reservedMemoryAvailableLocked() const noexcept {
  return config_.reserved_memory_capacity_bytes -
         reserved_memory_committed_bytes_;
}

ReleaseResult ReservationManager::releaseLocked(
    const std::string &job_id, ReservationRecord &reservation,
    std::uint64_t memory_current_bytes, std::uint64_t safety_margin_bytes) {
  const auto job = jobs_.find(job_id);
  if (job == jobs_.end()) {
    return {.decision = ReleaseDecision::Denied,
            .reservation = reservation,
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "job_not_registered"};
  }
  if (reservation.job_id != job_id) {
    return {.decision = ReleaseDecision::Denied,
            .reservation = reservation,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "token_owner_mismatch"};
  }
  if (reservation.state == ReservationState::Released) {
    return {.decision = ReleaseDecision::AlreadyReleased,
            .reservation = reservation,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "already_released"};
  }
  if (reservation.bytes > job->second.borrowed_bytes) {
    throw std::logic_error("reservation accounting underflow");
  }

  // RELEASE is also the workload's declaration that this operation will not
  // allocate any more of its original estimate. Clear the in-flight
  // commitment even when lowering memory.max must remain pending.
  reservation.remaining_estimated_bytes = 0;

  // A current-aware grant can require no extra cgroup capacity. Such a token
  // still brackets the workload operation, but releasing it cannot lower the
  // limit and therefore cannot become pending.
  if (reservation.bytes == 0) {
    reservation.state = ReservationState::Released;
    return {.decision = ReleaseDecision::Released,
            .reservation = reservation,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "released_no_borrow"};
  }

  std::uint64_t other_inflight_estimated = 0;
  for (const auto &[other_token, active] : reservations_) {
    if (other_token == reservation.token || active.job_id != job_id ||
        active.state != ReservationState::Active) {
      continue;
    }
    if (addWouldOverflow(other_inflight_estimated,
                         active.remaining_estimated_bytes)) {
      reservation.state = ReservationState::PendingRelease;
      return {.decision = ReleaseDecision::Pending,
              .reservation = reservation,
              .effective_limit_bytes = job->second.effectiveLimitBytes(),
              .reserved_memory_available_bytes =
                  reservedMemoryAvailableLocked(),
              .reason = "other_inflight_estimate_overflow"};
    }
    other_inflight_estimated += active.remaining_estimated_bytes;
  }

  // memory.current can still be low while another admitted worker is only
  // part-way through its allocation. Preserve observed usage, every other
  // active token's remaining commitment, and the safety margin. If the whole
  // token cannot be returned, lower memory.max as far as this boundary safely
  // permits instead of pinning the token's full original quota.
  if (addWouldOverflow(memory_current_bytes, other_inflight_estimated) ||
      addWouldOverflow(memory_current_bytes + other_inflight_estimated,
                       safety_margin_bytes)) {
    reservation.state = ReservationState::PendingRelease;
    return {.decision = ReleaseDecision::Pending,
            .reservation = reservation,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = "memory_usage_above_proposed_limit"};
  }

  const auto safe_limit = memory_current_bytes + other_inflight_estimated +
                          safety_margin_bytes;
  const auto effective_limit = job->second.effectiveLimitBytes();
  const auto limit_without_reservation = effective_limit - reservation.bytes;
  const auto required_retained_bytes =
      safe_limit > limit_without_reservation
          ? std::min(reservation.bytes, safe_limit - limit_without_reservation)
          : 0;
  const auto returned_bytes = reservation.bytes - required_retained_bytes;
  if (returned_bytes != 0) {
    const auto lowered_limit = effective_limit - returned_bytes;
    limit_writer_(job_id, lowered_limit + job->second.checkpoint_bytes);
    job->second.borrowed_bytes -= returned_bytes;
    reserved_memory_committed_bytes_ -= returned_bytes;
    reservation.bytes = required_retained_bytes;
  }

  if (reservation.bytes != 0) {
    reservation.state = ReservationState::PendingRelease;
    return {.decision = ReleaseDecision::Pending,
            .reservation = reservation,
            .effective_limit_bytes = job->second.effectiveLimitBytes(),
            .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
            .reason = returned_bytes == 0
                          ? "memory_usage_above_proposed_limit"
                          : "partially_released_to_safe_limit"};
  }

  reservation.state = ReservationState::Released;
  return {.decision = ReleaseDecision::Released,
          .reservation = reservation,
          .effective_limit_bytes = job->second.effectiveLimitBytes(),
          .reserved_memory_available_bytes = reservedMemoryAvailableLocked(),
          .reason = "released"};
}

std::string ReservationManager::nextTokenLocked() {
  return "rsv-" + std::to_string(next_token_id_++);
}

void ReservationManager::enqueuePendingLocked(
    const std::pair<std::string, std::string> &key,
    std::uint64_t estimated_bytes) {
  auto [entry, inserted] = pending_requests_.emplace(
      key, PendingReservationRecord{.job_id = key.first,
                                    .request_id = key.second,
                                    .estimated_bytes = estimated_bytes});
  if (!inserted) {
    return;
  }
  try {
    pending_order_.push_back(key);
  } catch (...) {
    pending_requests_.erase(entry);
    throw;
  }
}

void ReservationManager::erasePendingLocked(
    const std::pair<std::string, std::string> &key) {
  if (pending_requests_.erase(key) == 0) {
    return;
  }
  const auto position =
      std::find(pending_order_.begin(), pending_order_.end(), key);
  if (position != pending_order_.end()) {
    pending_order_.erase(position);
  }
}

void ReservationManager::erasePendingForJobLocked(const std::string &job_id) {
  for (auto position = pending_order_.begin();
       position != pending_order_.end();) {
    if (position->first == job_id) {
      pending_requests_.erase(*position);
      position = pending_order_.erase(position);
    } else {
      ++position;
    }
  }
}
