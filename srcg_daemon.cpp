#include "srcg_daemon.h"

#include <stdexcept>
#include <thread>

SrcgDaemon::SrcgDaemon(DaemonConfig config, CgroupBackend backend)
    : config_(std::move(config)), cgroups_(config_.cgroup_parent, backend) {
  if (config_.parent_memory_limit_bytes == 0) {
    throw std::invalid_argument("parent memory limit must be configured");
  }
  if (config_.monitoring_interval.count() <= 0 ||
      config_.cleanup_timeout.count() < 0) {
    throw std::invalid_argument("invalid monitoring or cleanup interval");
  }
}

void SrcgDaemon::initialize() {
  const auto host = linux_monitor_.sampleHostMemory();
  if (config_.host_headroom_bytes >= host.mem_total_bytes ||
      config_.parent_memory_limit_bytes >
          host.mem_total_bytes - config_.host_headroom_bytes) {
    throw std::runtime_error(
        "parent memory limit violates configured host headroom");
  }
  cgroups_.initializeParent(config_.parent_memory_limit_bytes,
                            config_.parent_swap_limit_bytes);
}

JobRunResult SrcgDaemon::runJob(const JobConfig &job,
                                SampleCallback on_sample) {
  if (job.memory_limit_bytes > config_.parent_memory_limit_bytes) {
    throw std::invalid_argument("job memory limit exceeds srcg parent limit");
  }
  if (job.swap_limit_bytes && config_.parent_swap_limit_bytes &&
      *job.swap_limit_bytes > *config_.parent_swap_limit_bytes) {
    throw std::invalid_argument(
        "job swap limit exceeds srcg parent swap limit");
  }
  cgroups_.createJob(job.job_id, job.memory_limit_bytes, job.swap_limit_bytes);
  try {
    ProcessSupervisor supervisor(cgroups_, config_.monitoring_interval);
    auto result = supervisor.run(
        job.job_id, job.command,
        [this, on_sample = std::move(on_sample)](const CgroupSnapshot &sample) {
          if (on_sample) {
            const auto timestamp =
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now().time_since_epoch())
                    .count();
            on_sample(ResourceSample{
                .monotonic_time_ms = static_cast<std::uint64_t>(timestamp),
                .job = sample,
                .host = linux_monitor_.sampleHostMemory()});
          }
        });
    const bool cleanup_safe = waitUntilCleanupSafe(job.job_id);
    if (cleanup_safe) {
      cgroups_.cleanupJob(job.job_id);
    }
    return {.process = std::move(result), .cgroup_cleaned = cleanup_safe};
  } catch (...) {
    if (waitUntilCleanupSafe(job.job_id)) {
      try {
        cgroups_.cleanupJob(job.job_id);
      } catch (...) {
        // Preserve the original launch/supervision error.  An orphaned
        // cgroup remains visible to `srcg status` and `srcg cleanup`.
      }
    }
    throw;
  }
}

CgroupSnapshot SrcgDaemon::jobStatus(const std::string &job_id) const {
  return cgroups_.snapshot(job_id);
}

void SrcgDaemon::cleanupJob(const std::string &job_id) {
  cgroups_.cleanupJob(job_id);
}

CgroupEnvironment SrcgDaemon::inspectEnvironment() const {
  return cgroups_.inspectEnvironment();
}

bool SrcgDaemon::waitUntilCleanupSafe(const std::string &job_id) const {
  const auto deadline =
      std::chrono::steady_clock::now() + config_.cleanup_timeout;
  do {
    if (cgroups_.cleanupSafe(job_id)) {
      return true;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds{25});
  } while (std::chrono::steady_clock::now() < deadline);
  return false;
}
