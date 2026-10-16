#ifndef SRCG_DAEMON_H
#define SRCG_DAEMON_H

#include "cgroup_manager.h"
#include "linux_monitor.h"
#include "process_supervisor.h"

#include <chrono>
#include <functional>
#include <string>
#include <vector>

struct DaemonConfig {
  std::filesystem::path cgroup_parent{"/sys/fs/cgroup/srcg"};
  std::chrono::milliseconds monitoring_interval{500};
  std::chrono::milliseconds cleanup_timeout{2000};
};

struct JobConfig {
  std::string job_id;
  std::uint64_t memory_limit_bytes{};
  MemoryLimit swap_limit_bytes{std::uint64_t{0}};
  std::vector<std::string> command;
};

struct JobRunResult {
  ProcessResult process;
  bool cgroup_cleaned{false};
};

struct ResourceSample {
  std::uint64_t monotonic_time_ms{};
  CgroupSnapshot job;
  HostMemorySnapshot host;
};

class SrcgDaemon {
public:
  using SampleCallback = std::function<void(const ResourceSample &)>;

  explicit SrcgDaemon(DaemonConfig config,
                      CgroupBackend backend = CgroupBackend::Kernel);

  [[nodiscard]] JobRunResult runJob(const JobConfig &job,
                                    SampleCallback on_sample = {});
  [[nodiscard]] CgroupSnapshot parentStatus() const;
  [[nodiscard]] CgroupSnapshot jobStatus(const std::string &job_id) const;
  void cleanupJob(const std::string &job_id);
  [[nodiscard]] CgroupEnvironment inspectEnvironment() const;

private:
  [[nodiscard]] bool waitUntilCleanupSafe(const std::string &job_id) const;

  DaemonConfig config_;
  LinuxMonitor linux_monitor_;
  CgroupV2Manager cgroups_;
};

#endif
