#include "srcg_daemon.h"

#include <stdexcept>
#include <thread>

SrcgDaemon::SrcgDaemon(DaemonConfig config, CgroupBackend backend)
    : config_(std::move(config)), cgroups_(config_.cgroup_parent, backend) {
  if (config_.monitoring_interval.count() <= 0 ||
      config_.cleanup_timeout.count() < 0) {
    throw std::invalid_argument("invalid monitoring or cleanup interval");
  }
}

JobRunResult SrcgDaemon::runJob(const JobConfig &job,
                                SampleCallback on_sample) {
  const auto parent = cgroups_.parentSnapshot();
  if (!parent.memory_max_bytes) {
    throw std::runtime_error(
        "srcg parent memory.max must be finite; configure it with `srcg init`");
  }
  if (job.memory_limit_bytes > *parent.memory_max_bytes) {
    throw std::invalid_argument("job memory limit exceeds srcg parent limit");
  }
  if (job.swap_limit_bytes && parent.swap_max_bytes &&
      *job.swap_limit_bytes > *parent.swap_max_bytes) {
    throw std::invalid_argument(
        "job swap limit exceeds srcg parent swap limit");
  }
  cgroups_.createJob(job.job_id, job.memory_limit_bytes, job.swap_limit_bytes);
  try {
    ProcessSupervisor supervisor(cgroups_, config_.monitoring_interval);
    ProcessSupervisor::SampleCallback supervisor_sample;
    if (on_sample) {
      supervisor_sample =
          [this, on_sample = std::move(on_sample)](
              const CgroupSnapshot &sample) {
            const auto timestamp =
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now().time_since_epoch())
                    .count();
            on_sample(ResourceSample{
                .monotonic_time_ms = static_cast<std::uint64_t>(timestamp),
                .job = sample,
                .host = linux_monitor_.sampleHostMemory()});
          };
    }
    auto result = supervisor.run(job.job_id, job.command,
                                 std::move(supervisor_sample));
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

CgroupSnapshot SrcgDaemon::parentStatus() const {
  return cgroups_.parentSnapshot();
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
