#include "process_supervisor.h"

#include <atomic>
#include <cerrno>
#include <csignal>
#include <cstring>
#include <stdexcept>
#include <sys/wait.h>
#include <system_error>
#include <thread>
#include <unistd.h>

namespace {
void closeNoInterrupt(int descriptor) noexcept {
  while (::close(descriptor) != 0 && errno == EINTR) {
  }
}

void waitForChild(pid_t pid) noexcept {
  int status = 0;
  while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {
  }
}

void writeBarrierByte(int descriptor, char value) {
  ssize_t written = 0;
  do {
    written = ::write(descriptor, &value, 1);
  } while (written < 0 && errno == EINTR);
  if (written != 1) {
    throw std::runtime_error("failed to release child exec barrier: " +
                             std::string(std::strerror(errno)));
  }
}

std::uint64_t eventIncrease(std::uint64_t before, std::uint64_t after) {
  return after >= before ? after - before : 0;
}
} // namespace

ProcessSupervisor::ProcessSupervisor(
    CgroupV2Manager &cgroups, std::chrono::milliseconds monitoring_interval)
    : cgroups_(cgroups), monitoring_interval_(monitoring_interval) {
  if (monitoring_interval_.count() <= 0) {
    throw std::invalid_argument("monitoring interval must be positive");
  }
}

ProcessResult ProcessSupervisor::run(const std::string &job_id,
                                     const std::vector<std::string> &command,
                                     SampleCallback on_sample) {
  if (command.empty() || command.front().empty()) {
    throw std::invalid_argument("job command must not be empty");
  }

  const auto initial = cgroups_.snapshot(job_id);
  int barrier[2]{};
  if (::pipe(barrier) != 0) {
    throw std::runtime_error("cannot create exec barrier: " +
                             std::string(std::strerror(errno)));
  }

  const pid_t pid = ::fork();
  if (pid < 0) {
    const auto message = std::string(std::strerror(errno));
    closeNoInterrupt(barrier[0]);
    closeNoInterrupt(barrier[1]);
    throw std::runtime_error("fork failed: " + message);
  }
  if (pid == 0) {
    closeNoInterrupt(barrier[1]);
    char permission = 0;
    ssize_t received = 0;
    do {
      received = ::read(barrier[0], &permission, 1);
    } while (received < 0 && errno == EINTR);
    closeNoInterrupt(barrier[0]);
    if (received != 1 || permission != 'G') {
      _exit(126);
    }

    std::vector<char *> arguments;
    arguments.reserve(command.size() + 1);
    for (const auto &argument : command) {
      arguments.push_back(const_cast<char *>(argument.c_str()));
    }
    arguments.push_back(nullptr);
    ::execvp(arguments.front(), arguments.data());
    _exit(127);
  }

  closeNoInterrupt(barrier[0]);
  try {
    // The child cannot exec until this write succeeds, so all descendants
    // inherit the job cgroup from their parent.
    cgroups_.attachProcess(job_id, pid);
    writeBarrierByte(barrier[1], 'G');
    closeNoInterrupt(barrier[1]);
  } catch (...) {
    closeNoInterrupt(barrier[1]);
    waitForChild(pid);
    throw;
  }

  std::atomic_bool monitoring{true};
  auto monitor = monitor_worker_.submit([&, on_sample] {
    while (monitoring.load(std::memory_order_acquire)) {
      if (on_sample) {
        on_sample(cgroups_.snapshot(job_id));
      }
      std::this_thread::sleep_for(monitoring_interval_);
    }
  });

  int status = 0;
  pid_t waited = -1;
  do {
    waited = ::waitpid(pid, &status, 0);
  } while (waited < 0 && errno == EINTR);
  monitoring.store(false, std::memory_order_release);
  monitor.get();
  if (waited < 0) {
    throw std::runtime_error("waitpid failed: " +
                             std::string(std::strerror(errno)));
  }

  auto final_snapshot = cgroups_.snapshot(job_id);
  ProcessResult result;
  result.pid = pid;
  result.final_snapshot = final_snapshot;
  result.cgroup_oom = eventIncrease(initial.events.oom_kill,
                                    final_snapshot.events.oom_kill) > 0 ||
                      eventIncrease(initial.events.oom_group_kill,
                                    final_snapshot.events.oom_group_kill) > 0;
  if (WIFEXITED(status)) {
    result.exit_code = WEXITSTATUS(status);
  } else if (WIFSIGNALED(status)) {
    result.termination_signal = WTERMSIG(status);
  }
  return result;
}
