#ifndef SRCG_RESERVATION_MANAGER_H
#define SRCG_RESERVATION_MANAGER_H

#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
#include <vector>

enum class ReservationState {
  Active,
  PendingRelease,
  Released,
};

enum class ReserveDecision {
  Granted,
  Wait,
  Denied,
};

enum class ReleaseDecision {
  Released,
  Pending,
  AlreadyReleased,
  Denied,
};

enum class ShrinkDecision {
  Shrunk,
  Unchanged,
  AlreadyReleased,
  Denied,
};

struct ReservationManagerConfig {
  std::uint64_t parent_limit_bytes{};
  std::uint64_t reserved_memory_capacity_bytes{};
  std::uint64_t checkpoint_budget_bytes{};
  std::uint64_t host_headroom_bytes{};
};

struct ReservationRecord {
  std::string token;
  std::string request_id;
  std::string job_id;
  // Workload-side estimate of new memory that the operation can allocate.
  std::uint64_t estimated_bytes{};
  // Portion of estimated_bytes that the current workload phase may still
  // allocate. This commitment closes the race between concurrent DataLoader
  // workers that reserve before either allocation appears in memory.current.
  std::uint64_t remaining_estimated_bytes{};
  // Capacity actually added to memory.max and charged to Reserved Memory.
  std::uint64_t bytes{};
  ReservationState state{ReservationState::Active};
};

struct PendingReservationRecord {
  std::string job_id;
  std::string request_id;
  std::uint64_t estimated_bytes{};
};

struct JobAccount {
  std::string job_id;
  std::uint64_t base_quota_bytes{};
  std::uint64_t borrowed_bytes{};
  std::uint64_t checkpoint_bytes{};

  [[nodiscard]] std::uint64_t effectiveLimitBytes() const noexcept {
    return base_quota_bytes + borrowed_bytes;
  }
  [[nodiscard]] std::uint64_t physicalLimitBytes() const noexcept {
    return effectiveLimitBytes() + checkpoint_bytes;
  }
};

struct CheckpointReservation {
  std::string token;
  std::string request_id;
  std::string job_id;
  std::uint64_t bytes{};
  ReservationState state{ReservationState::Active};
};

struct CheckpointResult {
  ReserveDecision decision{ReserveDecision::Denied};
  std::optional<CheckpointReservation> reservation;
  std::uint64_t effective_limit_bytes{};
  std::uint64_t available_bytes{};
  std::string reason;
};

struct CheckpointReleaseResult {
  ReleaseDecision decision{ReleaseDecision::Denied};
  std::uint64_t effective_limit_bytes{};
  std::uint64_t available_bytes{};
  std::string reason;
};

struct ReserveResult {
  ReserveDecision decision{ReserveDecision::Denied};
  std::optional<ReservationRecord> reservation;
  std::uint64_t effective_limit_bytes{};
  std::uint64_t borrowed_bytes{};
  std::uint64_t reserved_memory_available_bytes{};
  std::string reason;
};

struct ReleaseResult {
  ReleaseDecision decision{ReleaseDecision::Denied};
  std::optional<ReservationRecord> reservation;
  std::uint64_t effective_limit_bytes{};
  std::uint64_t reserved_memory_available_bytes{};
  std::string reason;
};

struct ShrinkResult {
  ShrinkDecision decision{ShrinkDecision::Denied};
  std::optional<ReservationRecord> reservation;
  std::uint64_t previous_borrowed_bytes{};
  std::uint64_t borrowed_bytes{};
  std::uint64_t returned_bytes{};
  std::uint64_t effective_limit_bytes{};
  std::uint64_t reserved_memory_available_bytes{};
  std::string reason;
};

struct ReservationManagerSnapshot {
  ReservationManagerConfig config;
  std::uint64_t base_committed_bytes{};
  std::uint64_t reserved_memory_committed_bytes{};
  std::uint64_t checkpoint_committed_bytes{};
  std::map<std::string, JobAccount> jobs;
  std::map<std::string, ReservationRecord> reservations;
  std::map<std::string, CheckpointReservation> checkpoints;
  std::vector<PendingReservationRecord> pending_reservations;

  [[nodiscard]] std::uint64_t reservedMemoryAvailableBytes() const noexcept {
    return config.reserved_memory_capacity_bytes -
           reserved_memory_committed_bytes;
  }
};

class ReservationManager {
public:
  using LimitWriter =
      std::function<void(const std::string &job_id, std::uint64_t new_limit)>;

  explicit ReservationManager(ReservationManagerConfig config,
                              LimitWriter limit_writer);

  void registerJob(const std::string &job_id, std::uint64_t base_quota_bytes);
  void unregisterJob(const std::string &job_id);

  [[nodiscard]] CheckpointResult beginCheckpoint(
      const std::string &job_id, const std::string &request_id,
      std::uint64_t memory_current_bytes,
      std::uint64_t host_mem_available_bytes,
      std::uint64_t safety_margin_bytes);
  [[nodiscard]] CheckpointReleaseResult endCheckpoint(
      const std::string &job_id, const std::string &token,
      std::uint64_t memory_current_bytes,
      std::uint64_t safety_margin_bytes);

  [[nodiscard]] ReserveResult reserve(const std::string &job_id,
                                      const std::string &request_id,
                                      std::uint64_t estimated_allocation_bytes,
                                      std::uint64_t memory_current_bytes,
                                      std::uint64_t host_mem_available_bytes,
                                      std::uint64_t inactive_file_bytes = 0,
                                      std::uint64_t safety_margin_bytes = 0);

  // Remove a request that the client will no longer retry. This keeps a
  // timed-out or interrupted client from blocking the FIFO admission queue.
  [[nodiscard]] bool cancelPending(const std::string &job_id,
                                   const std::string &request_id);

  // Reduce an active reservation after a workload phase has discarded its
  // temporary allocations. remaining_allocation_bytes is a workload estimate
  // of memory that may still be allocated before the final release; the
  // manager remains authoritative and never lowers memory.max below the
  // observed memory.current plus the configured safety margin.
  [[nodiscard]] ShrinkResult shrink(
      const std::string &job_id, const std::string &token,
      std::uint64_t remaining_allocation_bytes,
      std::uint64_t memory_current_bytes, std::uint64_t inactive_file_bytes,
      std::uint64_t safety_margin_bytes);

  [[nodiscard]] ReleaseResult release(const std::string &job_id,
                                      const std::string &token,
                                      std::uint64_t memory_current_bytes,
                                      std::uint64_t safety_margin_bytes);

  [[nodiscard]] std::vector<ReleaseResult>
  retryPendingReleases(const std::string &job_id,
                       std::uint64_t memory_current_bytes,
                       std::uint64_t safety_margin_bytes);

  // The caller must establish that the job cgroup is empty before using this
  // crash-recovery operation. It returns every outstanding reservation to the
  // Reserved Memory without lowering a limit on a live process tree.
  void reclaimEmptyJob(const std::string &job_id);

  [[nodiscard]] ReservationManagerSnapshot snapshot() const;

private:
  [[nodiscard]] std::uint64_t reservedMemoryAvailableLocked() const noexcept;
  [[nodiscard]] ReleaseResult releaseLocked(const std::string &job_id,
                                            ReservationRecord &reservation,
                                            std::uint64_t memory_current_bytes,
                                            std::uint64_t safety_margin_bytes);
  [[nodiscard]] std::string nextTokenLocked();
  void enqueuePendingLocked(const std::pair<std::string, std::string> &key,
                            std::uint64_t estimated_bytes);
  void erasePendingLocked(const std::pair<std::string, std::string> &key);
  void erasePendingForJobLocked(const std::string &job_id);

  ReservationManagerConfig config_;
  LimitWriter limit_writer_;
  mutable std::mutex mutex_;
  std::map<std::string, JobAccount> jobs_;
  std::map<std::string, ReservationRecord> reservations_;
  std::map<std::pair<std::string, std::string>, std::string> request_tokens_;
  std::map<std::pair<std::string, std::string>, PendingReservationRecord>
      pending_requests_;
  std::deque<std::pair<std::string, std::string>> pending_order_;
  std::uint64_t base_committed_bytes_{};
  std::uint64_t reserved_memory_committed_bytes_{};
  std::uint64_t checkpoint_committed_bytes_{};
  std::map<std::string, CheckpointReservation> checkpoints_;
  std::map<std::pair<std::string, std::string>, std::string> checkpoint_requests_;
  std::uint64_t next_token_id_{1};
};

#endif
