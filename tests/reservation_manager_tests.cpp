#include "reservation_manager.h"

#include <atomic>
#include <barrier>
#include <cassert>
#include <cstdint>
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {
constexpr std::uint64_t mib = 1024ULL * 1024ULL;

class FakeLimits {
public:
  void write(const std::string &job_id, std::uint64_t limit) {
    std::lock_guard lock(mutex_);
    limits_[job_id] = limit;
    ++writes_;
  }

  [[nodiscard]] std::uint64_t get(const std::string &job_id) const {
    std::lock_guard lock(mutex_);
    return limits_.at(job_id);
  }

  [[nodiscard]] std::uint64_t writes() const noexcept { return writes_.load(); }

private:
  mutable std::mutex mutex_;
  std::map<std::string, std::uint64_t> limits_;
  std::atomic_uint64_t writes_{};
};
} // namespace

int main() {
  bool bad_budget_rejected = false;
  try {
    ReservationManager invalid({.parent_limit_bytes = 100 * mib,
                                .reserved_memory_capacity_bytes = 80 * mib,
                                .checkpoint_budget_bytes = 30 * mib},
                               [](const std::string &, std::uint64_t) {});
  } catch (const std::invalid_argument &) {
    bad_budget_rejected = true;
  }
  assert(bad_budget_rejected);

  FakeLimits limits;
  ReservationManager manager(
      {.parent_limit_bytes = 1024 * mib,
       .reserved_memory_capacity_bytes = 256 * mib,
       .checkpoint_budget_bytes = 128 * mib,
       .host_headroom_bytes = 64 * mib},
      [&limits](const std::string &job_id, std::uint64_t limit) {
        limits.write(job_id, limit);
      });

  manager.registerJob("job-A", 256 * mib);
  manager.registerJob("job-A", 256 * mib); // idempotent
  manager.registerJob("job-B", 256 * mib);
  bool base_overcommit_rejected = false;
  try {
    manager.registerJob("job-C", 256 * mib);
  } catch (const std::runtime_error &) {
    base_overcommit_rejected = true;
  }
  assert(base_overcommit_rejected);

  const auto granted =
      manager.reserve("job-A", "request-A", 128 * mib, 256 * mib, 1024 * mib);
  assert(granted.decision == ReserveDecision::Granted);
  assert(granted.reservation.has_value());
  assert(limits.get("job-A") == 384 * mib);
  assert(manager.snapshot().reserved_memory_committed_bytes == 128 * mib);

  const auto duplicate =
      manager.reserve("job-A", "request-A", 128 * mib, 384 * mib, 1024 * mib);
  assert(duplicate.decision == ReserveDecision::Granted);
  assert(duplicate.reservation->token == granted.reservation->token);
  assert(limits.writes() == 1);

  const auto conflict =
      manager.reserve("job-A", "request-A", 64 * mib, 384 * mib, 1024 * mib);
  assert(conflict.decision == ReserveDecision::Denied);
  assert(conflict.reason == "request_id_conflict");

  const auto host_wait =
      manager.reserve("job-B", "request-host", 64 * mib, 256 * mib, 100 * mib);
  assert(host_wait.decision == ReserveDecision::Wait);
  assert(host_wait.reason == "host_headroom");
  assert(manager.cancelPending("job-B", "request-host"));

  const auto reserved_memory_wait = manager.reserve(
      "job-B", "request-pool", 192 * mib, 256 * mib, 1024 * mib);
  assert(reserved_memory_wait.decision == ReserveDecision::Wait);
  assert(reserved_memory_wait.reason == "reserved_memory_exhausted");
  assert(manager.cancelPending("job-B", "request-pool"));

  const auto impossible = manager.reserve("job-B", "request-too-large",
                                          513 * mib, 256 * mib, 1024 * mib);
  assert(impossible.decision == ReserveDecision::Denied);
  assert(impossible.reason == "estimated_allocation_exceeds_job_capacity");

  // Request IDs are scoped to their owning job rather than globally.
  const auto same_request_other_job =
      manager.reserve("job-B", "request-A", 64 * mib, 256 * mib, 1024 * mib);
  assert(same_request_other_job.decision == ReserveDecision::Granted);
  assert(same_request_other_job.reservation->token !=
         granted.reservation->token);
  const auto release_other = manager.release(
      "job-B", same_request_other_job.reservation->token, 200 * mib, 32 * mib);
  assert(release_other.decision == ReleaseDecision::Released);

  const auto pending =
      manager.release("job-A", granted.reservation->token, 300 * mib, 32 * mib);
  assert(pending.decision == ReleaseDecision::Pending);
  assert(pending.reason == "partially_released_to_safe_limit");
  assert(pending.reservation->bytes == 76 * mib);
  assert(manager.snapshot().reserved_memory_committed_bytes == 76 * mib);
  assert(limits.get("job-A") == 332 * mib);

  const auto pending_retry =
      manager.retryPendingReleases("job-A", 200 * mib, 32 * mib);
  assert(pending_retry.size() == 1);
  assert(pending_retry.front().decision == ReleaseDecision::Released);
  assert(limits.get("job-A") == 256 * mib);
  assert(manager.snapshot().reserved_memory_committed_bytes == 0);

  const auto duplicate_release =
      manager.release("job-A", granted.reservation->token, 200 * mib, 32 * mib);
  assert(duplicate_release.decision == ReleaseDecision::AlreadyReleased);
  manager.unregisterJob("job-A");

  // RESERVE polling must re-evaluate pending releases itself. Otherwise a
  // waiter can hold the only thread that would have sent a separate RETRY and
  // leave reclaimable Reserved Memory pinned forever.
  FakeLimits auto_retry_limits;
  ReservationManager auto_retry(
      {.parent_limit_bytes = 512 * mib,
       .reserved_memory_capacity_bytes = 256 * mib,
       .checkpoint_budget_bytes = 0,
       .host_headroom_bytes = 0},
      [&auto_retry_limits](const std::string &job_id, std::uint64_t limit) {
        auto_retry_limits.write(job_id, limit);
      });
  auto_retry.registerJob("job", 256 * mib);
  const auto auto_holder =
      auto_retry.reserve("job", "holder", 128 * mib, 256 * mib, 1024 * mib);
  assert(auto_holder.decision == ReserveDecision::Granted);
  const auto auto_pending = auto_retry.release(
      "job", auto_holder.reservation->token, 300 * mib, 32 * mib);
  assert(auto_pending.decision == ReleaseDecision::Pending);
  assert(auto_pending.reservation->bytes == 76 * mib);
  const auto after_auto_retry =
      auto_retry.reserve("job", "next", 64 * mib, 200 * mib, 1024 * mib,
                         0, 32 * mib);
  assert(after_auto_retry.decision == ReserveDecision::Granted);
  assert(auto_retry.snapshot()
             .reservations.at(auto_holder.reservation->token)
             .state == ReservationState::Released);
  assert(after_auto_retry.borrowed_bytes == 40 * mib);
  assert(auto_retry.release("job", after_auto_retry.reservation->token,
                            200 * mib, 0)
             .decision == ReleaseDecision::Released);

  // Current-aware borrowing charges only the part of an estimate that does
  // not fit below the job's existing effective limit. Host admission still
  // checks the complete estimate, not this reduced accounting amount.
  FakeLimits aware_limits;
  ReservationManager aware(
      {.parent_limit_bytes = 1024 * mib,
       .reserved_memory_capacity_bytes = 256 * mib,
       .checkpoint_budget_bytes = 0,
       .host_headroom_bytes = 64 * mib},
      [&aware_limits](const std::string &job_id, std::uint64_t limit) {
        aware_limits.write(job_id, limit);
      });
  aware.registerJob("job", 256 * mib);
  const auto partial =
      aware.reserve("job", "partial", 200 * mib, 100 * mib, 1024 * mib);
  assert(partial.decision == ReserveDecision::Granted);
  assert(partial.borrowed_bytes == 44 * mib);
  assert(partial.reservation->estimated_bytes == 200 * mib);
  assert(partial.reservation->bytes == 44 * mib);
  assert(partial.effective_limit_bytes == 300 * mib);
  assert(aware_limits.get("job") == 300 * mib);
  assert(
      aware.release("job", partial.reservation->token, 100 * mib, 0).decision ==
      ReleaseDecision::Released);

  // Phase-aware shrinking returns only the part of a token that both the
  // remaining workload estimate and the observed cgroup usage no longer need.
  const auto shrink_source =
      aware.reserve("job", "shrink", 200 * mib, 256 * mib, 1024 * mib);
  assert(shrink_source.decision == ReserveDecision::Granted);
  assert(shrink_source.borrowed_bytes == 200 * mib);
  const auto shrunk = aware.shrink("job", shrink_source.reservation->token,
                                   20 * mib, 300 * mib, 0, 20 * mib);
  assert(shrunk.decision == ShrinkDecision::Shrunk);
  assert(shrunk.previous_borrowed_bytes == 200 * mib);
  assert(shrunk.borrowed_bytes == 64 * mib);
  assert(shrunk.returned_bytes == 136 * mib);
  assert(shrunk.effective_limit_bytes == 320 * mib);
  assert(aware_limits.get("job") == 320 * mib);
  assert(aware.snapshot().reserved_memory_committed_bytes == 64 * mib);

  const auto shrink_cannot_grow = aware.shrink(
      "job", shrink_source.reservation->token, 201 * mib, 300 * mib, 0, 0);
  assert(shrink_cannot_grow.decision == ShrinkDecision::Denied);
  assert(shrink_cannot_grow.reason == "remaining_estimate_increase");
  assert(aware.release("job", shrink_source.reservation->token, 256 * mib, 0)
             .decision == ReleaseDecision::Released);

  // inactive_file can reduce the remaining-work estimate, but hard-limit
  // lowering still uses complete memory.current. The result is a partial safe
  // shrink rather than lowering memory.max below live cgroup usage.
  const auto partial_shrink_source =
      aware.reserve("job", "partial-shrink", 200 * mib, 256 * mib,
                    1024 * mib);
  assert(partial_shrink_source.decision == ReserveDecision::Granted);
  const auto partial_shrink = aware.shrink(
      "job", partial_shrink_source.reservation->token, 0, 400 * mib,
      200 * mib, 0);
  assert(partial_shrink.decision == ShrinkDecision::Shrunk);
  assert(partial_shrink.borrowed_bytes == 144 * mib);
  assert(partial_shrink.returned_bytes == 56 * mib);
  assert(partial_shrink.reason == "partially_shrunk_to_safe_limit");
  assert(aware_limits.get("job") == 400 * mib);
  assert(aware.release("job", partial_shrink_source.reservation->token,
                       256 * mib, 0)
             .decision == ReleaseDecision::Released);

  // An estimate can be larger than the pool when existing base headroom means
  // the amount actually borrowed still fits.
  const auto estimate_above_pool =
      aware.reserve("job", "above-pool", 300 * mib, 0, 1024 * mib);
  assert(estimate_above_pool.decision == ReserveDecision::Granted);
  assert(estimate_above_pool.borrowed_bytes == 44 * mib);
  assert(aware.release("job", estimate_above_pool.reservation->token, 0, 0)
             .decision == ReleaseDecision::Released);

  const auto writes_before_zero_borrow = aware_limits.writes();
  const auto zero =
      aware.reserve("job", "zero", 100 * mib, 100 * mib, 1024 * mib);
  assert(zero.decision == ReserveDecision::Granted);
  assert(zero.borrowed_bytes == 0);
  assert(zero.effective_limit_bytes == 256 * mib);
  assert(aware_limits.writes() == writes_before_zero_borrow);
  const auto zero_release =
      aware.release("job", zero.reservation->token, 300 * mib, 64 * mib);
  assert(zero_release.decision == ReleaseDecision::Released);
  assert(zero_release.reason == "released_no_borrow");
  assert(aware_limits.writes() == writes_before_zero_borrow);

  // Admission keeps one explicit margin above the observed usage plus all
  // in-flight estimates. This covers worker/process growth between the
  // memory.current snapshot and the actual allocation.
  const auto admission_margin = aware.reserve(
      "job", "admission-margin", 100 * mib, 100 * mib, 1024 * mib, 0,
      64 * mib);
  assert(admission_margin.decision == ReserveDecision::Granted);
  assert(admission_margin.borrowed_bytes == 8 * mib);
  assert(admission_margin.effective_limit_bytes == 264 * mib);
  assert(aware.release("job", admission_margin.reservation->token, 100 * mib,
                       0)
             .decision == ReleaseDecision::Released);

  const auto host_checks_full_estimate = aware.reserve(
      "job", "host-full-estimate", 150 * mib, 200 * mib, 200 * mib);
  assert(host_checks_full_estimate.borrowed_bytes == 94 * mib);
  assert(host_checks_full_estimate.decision == ReserveDecision::Wait);
  assert(host_checks_full_estimate.reason == "host_headroom");
  assert(aware.cancelPending("job", "host-full-estimate"));

  const auto cache_aware = aware.reserve("job", "inactive-file", 100 * mib,
                                         400 * mib, 1024 * mib, 200 * mib);
  assert(cache_aware.decision == ReserveDecision::Granted);
  // protected current = 400 - 200 MiB; base headroom = 56 MiB.
  assert(cache_aware.borrowed_bytes == 44 * mib);
  assert(cache_aware.effective_limit_bytes == 300 * mib);
  assert(aware.release("job", cache_aware.reservation->token, 200 * mib, 0)
             .decision == ReleaseDecision::Released);

  // Concurrent workers can receive tokens before either allocation appears
  // in memory.current. The second request must include the first token's
  // in-flight estimate instead of reusing the same base headroom twice.
  FakeLimits inflight_limits;
  ReservationManager inflight(
      {.parent_limit_bytes = 512 * mib,
       .reserved_memory_capacity_bytes = 256 * mib,
       .checkpoint_budget_bytes = 0,
       .host_headroom_bytes = 0},
      [&inflight_limits](const std::string &job_id, std::uint64_t limit) {
        inflight_limits.write(job_id, limit);
      });
  inflight.registerJob("job", 256 * mib);
  const auto first_worker =
      inflight.reserve("job", "worker-1", 100 * mib, 100 * mib, 1024 * mib);
  assert(first_worker.decision == ReserveDecision::Granted);
  assert(first_worker.borrowed_bytes == 0);
  assert(first_worker.reservation->remaining_estimated_bytes == 100 * mib);
  const auto second_worker =
      inflight.reserve("job", "worker-2", 100 * mib, 100 * mib, 1024 * mib);
  assert(second_worker.decision == ReserveDecision::Granted);
  assert(second_worker.borrowed_bytes == 44 * mib);
  assert(inflight_limits.get("job") == 300 * mib);

  const auto first_worker_shrink = inflight.shrink(
      "job", first_worker.reservation->token, 10 * mib, 100 * mib, 0, 0);
  assert(first_worker_shrink.decision == ShrinkDecision::Unchanged);
  assert(first_worker_shrink.reason == "estimate_shrunk_without_borrow");
  assert(inflight.snapshot()
             .reservations.at(first_worker.reservation->token)
             .remaining_estimated_bytes == 10 * mib);
  assert(inflight.release("job", second_worker.reservation->token, 100 * mib, 0)
             .decision == ReleaseDecision::Released);
  assert(inflight.release("job", first_worker.reservation->token, 100 * mib, 0)
             .decision == ReleaseDecision::Released);

  // A release may race with another worker that was granted but has not yet
  // materialized its complete allocation in memory.current. Do not remove an
  // older token's quota while that other in-flight commitment still needs it.
  FakeLimits release_race_limits;
  ReservationManager release_race(
      {.parent_limit_bytes = 768 * mib,
       .reserved_memory_capacity_bytes = 512 * mib,
       .checkpoint_budget_bytes = 0,
       .host_headroom_bytes = 0},
      [&release_race_limits](const std::string &job_id, std::uint64_t limit) {
        release_race_limits.write(job_id, limit);
      });
  release_race.registerJob("job", 256 * mib);
  const auto older =
      release_race.reserve("job", "older", 100 * mib, 256 * mib, 1024 * mib);
  assert(older.decision == ReserveDecision::Granted);
  const auto loading =
      release_race.reserve("job", "loading", 200 * mib, 300 * mib,
                           1024 * mib);
  assert(loading.decision == ReserveDecision::Granted);
  const auto release_during_load = release_race.release(
      "job", older.reservation->token, 400 * mib, 0);
  assert(release_during_load.decision == ReleaseDecision::Pending);
  assert(release_during_load.reason == "memory_usage_above_proposed_limit");
  assert(release_race_limits.get("job") == loading.effective_limit_bytes);

  // A FIFO request keeps its estimate but recomputes the amount to borrow
  // from the latest memory.current when it reaches the head of the queue.
  FakeLimits retry_limits;
  ReservationManager retry_aware(
      {.parent_limit_bytes = 300 * mib,
       .reserved_memory_capacity_bytes = 100 * mib,
       .checkpoint_budget_bytes = 0,
       .host_headroom_bytes = 0},
      [&retry_limits](const std::string &job_id, std::uint64_t limit) {
        retry_limits.write(job_id, limit);
      });
  retry_aware.registerJob("job-A", 100 * mib);
  retry_aware.registerJob("job-B", 100 * mib);
  const auto retry_holder =
      retry_aware.reserve("job-A", "holder", 80 * mib, 100 * mib, 1024 * mib);
  assert(retry_holder.decision == ReserveDecision::Granted);
  const auto retry_waiter =
      retry_aware.reserve("job-B", "waiter", 80 * mib, 100 * mib, 1024 * mib);
  assert(retry_waiter.decision == ReserveDecision::Wait);
  assert(retry_waiter.borrowed_bytes == 80 * mib);
  assert(retry_aware
             .release("job-A", retry_holder.reservation->token, 100 * mib, 0)
             .decision == ReleaseDecision::Released);
  const auto retry_granted =
      retry_aware.reserve("job-B", "waiter", 80 * mib, 50 * mib, 1024 * mib);
  assert(retry_granted.decision == ReserveDecision::Granted);
  assert(retry_granted.borrowed_bytes == 30 * mib);
  assert(retry_granted.reservation->estimated_bytes == 80 * mib);

  // Concurrency: only the requests that fit in the pool can be granted, even
  // when all callers begin at the same barrier.
  FakeLimits concurrent_limits;
  ReservationManager concurrent(
      {.parent_limit_bytes = 2048 * mib,
       .reserved_memory_capacity_bytes = 256 * mib,
       .checkpoint_budget_bytes = 0,
       .host_headroom_bytes = 0},
      [&concurrent_limits](const std::string &job_id, std::uint64_t limit) {
        concurrent_limits.write(job_id, limit);
      });
  constexpr int thread_count = 16;
  for (int index = 0; index < thread_count; ++index) {
    concurrent.registerJob("job-" + std::to_string(index), 32 * mib);
  }
  std::barrier start(thread_count);
  std::atomic_int grant_count{};
  std::vector<std::thread> callers;
  callers.reserve(thread_count);
  for (int index = 0; index < thread_count; ++index) {
    callers.emplace_back([&, index] {
      start.arrive_and_wait();
      const auto result = concurrent.reserve(
          "job-" + std::to_string(index), "concurrent-" + std::to_string(index),
          32 * mib, 32 * mib, 4096 * mib);
      if (result.decision == ReserveDecision::Granted) {
        ++grant_count;
      }
    });
  }
  for (auto &caller : callers) {
    caller.join();
  }
  assert(grant_count == 8);
  const auto concurrent_snapshot = concurrent.snapshot();
  assert(concurrent_snapshot.reserved_memory_committed_bytes == 256 * mib);
  assert(concurrent_snapshot.reservedMemoryAvailableBytes() == 0);
  std::uint64_t concurrent_borrowed = 0;
  for (const auto &[job_id, account] : concurrent_snapshot.jobs) {
    (void)job_id;
    concurrent_borrowed += account.borrowed_bytes;
  }
  assert(concurrent_borrowed == 256 * mib);

  for (int index = 0; index < thread_count; ++index) {
    concurrent.reclaimEmptyJob("job-" + std::to_string(index));
  }
  const auto reclaimed = concurrent.snapshot();
  assert(reclaimed.reserved_memory_committed_bytes == 0);
  assert(reclaimed.base_committed_bytes == 0);
  assert(reclaimed.jobs.empty());

  // FIFO fairness: a job that just released capacity cannot reacquire it in
  // front of an older waiter. Cancellation removes an abandoned FIFO head.
  FakeLimits fair_limits;
  ReservationManager fair(
      {.parent_limit_bytes = 400 * mib,
       .reserved_memory_capacity_bytes = 100 * mib,
       .checkpoint_budget_bytes = 0,
       .host_headroom_bytes = 0},
      [&fair_limits](const std::string &job_id, std::uint64_t limit) {
        fair_limits.write(job_id, limit);
      });
  fair.registerJob("job-A", 100 * mib);
  fair.registerJob("job-B", 100 * mib);
  const auto fair_a =
      fair.reserve("job-A", "a-1", 60 * mib, 100 * mib, 1024 * mib);
  assert(fair_a.decision == ReserveDecision::Granted);
  const auto fair_b_wait =
      fair.reserve("job-B", "b-1", 60 * mib, 100 * mib, 1024 * mib);
  assert(fair_b_wait.decision == ReserveDecision::Wait);
  const auto fair_a_cannot_bypass =
      fair.reserve("job-A", "a-2", 20 * mib, 160 * mib, 1024 * mib);
  assert(fair_a_cannot_bypass.decision == ReserveDecision::Wait);
  assert(fair_a_cannot_bypass.reason == "queued_behind_earlier_request");
  const auto fair_a_release =
      fair.release("job-A", fair_a.reservation->token, 100 * mib, 0);
  assert(fair_a_release.decision == ReleaseDecision::Released);
  const auto fair_a_still_waits =
      fair.reserve("job-A", "a-2", 20 * mib, 100 * mib, 1024 * mib);
  assert(fair_a_still_waits.decision == ReserveDecision::Wait);
  const auto fair_b_granted =
      fair.reserve("job-B", "b-1", 60 * mib, 100 * mib, 1024 * mib);
  assert(fair_b_granted.decision == ReserveDecision::Granted);
  assert(fair.snapshot().pending_reservations.size() == 1);
  assert(fair.cancelPending("job-A", "a-2"));
  assert(!fair.cancelPending("job-A", "a-2"));
  assert(fair.snapshot().pending_reservations.empty());

  // A failed cgroup write must not commit accounting or publish a token.
  ReservationManager failing({.parent_limit_bytes = 512 * mib,
                              .reserved_memory_capacity_bytes = 128 * mib,
                              .checkpoint_budget_bytes = 0,
                              .host_headroom_bytes = 0},
                             [](const std::string &, std::uint64_t) {
                               throw std::runtime_error(
                                   "simulated cgroup write failure");
                             });
  failing.registerJob("job", 128 * mib);
  bool write_failed = false;
  try {
    (void)failing.reserve("job", "request", 64 * mib, 128 * mib, 1024 * mib);
  } catch (const std::runtime_error &) {
    write_failed = true;
  }
  assert(write_failed);
  assert(failing.snapshot().reserved_memory_committed_bytes == 0);
  assert(failing.snapshot().reservations.empty());

  // Checkpoint quota is separate from batch borrowing and belongs to only
  // one saver at a time. A pending release must keep its cgroup limit intact.
  FakeLimits checkpoint_limits;
  ReservationManager checkpoints(
      {.parent_limit_bytes = 640 * mib,
       .reserved_memory_capacity_bytes = 128 * mib,
       .checkpoint_budget_bytes = 128 * mib,
       .host_headroom_bytes = 0},
      [&checkpoint_limits](const std::string &id, std::uint64_t limit) {
        checkpoint_limits.write(id, limit);
      });
  checkpoints.registerJob("save-A", 192 * mib);
  checkpoints.registerJob("save-B", 192 * mib);
  const auto batch = checkpoints.reserve(
      "save-A", "batch", 64 * mib, 192 * mib, 1024 * mib);
  assert(batch.decision == ReserveDecision::Granted);
  const auto first_save = checkpoints.beginCheckpoint(
      "save-A", "save-1", 256 * mib, 1024 * mib, 0);
  assert(first_save.decision == ReserveDecision::Granted);
  assert(checkpoint_limits.get("save-A") == 384 * mib);
  assert(checkpoints.snapshot().checkpoint_committed_bytes == 128 * mib);
  const auto duplicate_save = checkpoints.beginCheckpoint(
      "save-A", "save-1", 256 * mib, 1024 * mib, 0);
  assert(duplicate_save.reservation->token == first_save.reservation->token);
  assert(checkpoints.beginCheckpoint("save-B", "save-1", 192 * mib,
                                     1024 * mib, 0).decision ==
         ReserveDecision::Wait);
  const auto inflight_pending = checkpoints.endCheckpoint(
      "save-A", first_save.reservation->token, 240 * mib, 0);
  assert(inflight_pending.decision == ReleaseDecision::Pending);
  assert(checkpoint_limits.get("save-A") == 384 * mib);
  const auto pending_save = checkpoints.endCheckpoint(
      "save-A", first_save.reservation->token, 350 * mib, 0);
  assert(pending_save.decision == ReleaseDecision::Pending);
  assert(checkpoint_limits.get("save-A") == 384 * mib);
  assert(checkpoints.beginCheckpoint("save-A", "save-1", 350 * mib,
                                     1024 * mib, 0).reason ==
         "request_already_ended");
  const auto next_save = checkpoints.beginCheckpoint(
      "save-A", "save-2", 350 * mib, 1024 * mib, 0);
  assert(next_save.decision == ReserveDecision::Granted);
  assert(next_save.reservation->token != first_save.reservation->token);
  assert(checkpoints.endCheckpoint("save-A", first_save.reservation->token,
                                   180 * mib, 0).decision ==
         ReleaseDecision::AlreadyReleased);
  assert(checkpoint_limits.get("save-A") == 384 * mib);
  assert(checkpoints.endCheckpoint("save-A", next_save.reservation->token,
                                   180 * mib, 0).decision ==
         ReleaseDecision::Released);
  assert(checkpoint_limits.get("save-A") == 256 * mib);
  assert(checkpoints.snapshot().checkpoint_committed_bytes == 0);
  const auto second_save = checkpoints.beginCheckpoint(
      "save-B", "save-1", 192 * mib, 1024 * mib, 0);
  assert(second_save.decision == ReserveDecision::Granted);
  checkpoints.reclaimEmptyJob("save-B");
  assert(checkpoints.snapshot().checkpoint_committed_bytes == 0);
  assert(checkpoints.release("save-A", batch.reservation->token,
                             192 * mib, 0).decision ==
         ReleaseDecision::Released);
  checkpoints.reclaimEmptyJob("save-A");

  ReservationManager failing_checkpoint(
      {.parent_limit_bytes = 384 * mib,
       .reserved_memory_capacity_bytes = 0,
       .checkpoint_budget_bytes = 128 * mib},
      [](const std::string &, std::uint64_t) {
        throw std::runtime_error("simulated checkpoint limit failure");
      });
  failing_checkpoint.registerJob("job", 256 * mib);
  bool checkpoint_write_failed = false;
  try {
    (void)failing_checkpoint.beginCheckpoint("job", "save", 256 * mib,
                                             1024 * mib, 0);
  } catch (const std::runtime_error &) {
    checkpoint_write_failed = true;
  }
  assert(checkpoint_write_failed);
  assert(failing_checkpoint.snapshot().checkpoint_committed_bytes == 0);
  assert(failing_checkpoint.snapshot().checkpoints.empty());
}
