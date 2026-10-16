#ifndef SRCG_RESERVATION_IPC_H
#define SRCG_RESERVATION_IPC_H

#include "cgroup_manager.h"
#include "linux_monitor.h"
#include "reservation_manager.h"

#include <atomic>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string>

struct ReservationServiceConfig {
  std::filesystem::path cgroup_parent;
  std::filesystem::path socket_path;
  std::filesystem::path log_path;
  std::uint64_t reserved_memory_capacity_bytes{};
  std::uint64_t checkpoint_budget_bytes{};
  std::uint64_t host_headroom_bytes{};
  bool verify_peer_cgroup{true};
};

class ReservationService {
public:
  explicit ReservationService(ReservationServiceConfig config,
                              CgroupBackend backend = CgroupBackend::Kernel);
  ~ReservationService();

  ReservationService(const ReservationService &) = delete;
  ReservationService &operator=(const ReservationService &) = delete;

  // Foreground service. SIGINT/SIGTERM stop the accept loop cleanly.
  void serve();

private:
  [[nodiscard]] std::string handleRequest(const std::string &request,
                                          pid_t peer_pid, uid_t peer_uid);
  [[nodiscard]] bool peerBelongsToJob(pid_t peer_pid,
                                      const std::string &job_id) const;
  void logEvent(const std::string &event);

  ReservationServiceConfig config_;
  CgroupV2Manager cgroups_;
  LinuxMonitor monitor_;
  std::unique_ptr<ReservationManager> reservations_;
  std::ofstream log_;
  int listener_{-1};
  bool owns_socket_{false};
  std::atomic_bool stop_requested_{false};
};

// Send one newline-delimited protocol request and return one response line.
[[nodiscard]] std::string
sendReservationRequest(const std::filesystem::path &socket_path,
                       const std::string &request);

#endif
