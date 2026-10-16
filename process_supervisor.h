#ifndef SRCG_PROCESS_SUPERVISOR_H
#define SRCG_PROCESS_SUPERVISOR_H

#include "cgroup_manager.h"
#include "thread_manager.h"

#include <chrono>
#include <functional>
#include <optional>
#include <string>
#include <sys/types.h>
#include <vector>

struct ProcessResult {
  pid_t pid{-1};
  std::optional<int> exit_code;
  std::optional<int> termination_signal;
  bool cgroup_oom{false};
  CgroupSnapshot final_snapshot;

  [[nodiscard]] bool succeeded() const noexcept {
    return exit_code && *exit_code == 0;
  }
};

class ProcessSupervisor {
public:
  using SampleCallback = std::function<void(const CgroupSnapshot &)>;

  explicit ProcessSupervisor(CgroupV2Manager &cgroups,
                             std::chrono::milliseconds monitoring_interval =
                                 std::chrono::milliseconds{500});

  [[nodiscard]] ProcessResult run(const std::string &job_id,
                                  const std::vector<std::string> &command,
                                  SampleCallback on_sample = {});

private:
  CgroupV2Manager &cgroups_;
  std::chrono::milliseconds monitoring_interval_;
  ThreadManager monitor_worker_{1};
};

#endif
