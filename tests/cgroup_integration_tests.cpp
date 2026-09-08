#include "cgroup_manager.h"
#include "process_supervisor.h"

#include <cassert>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <sys/wait.h>
#include <thread>
#include <vector>

namespace {
int allocateChild(std::size_t bytes) {
  std::vector<unsigned char> allocation(bytes);
  for (std::size_t offset = 0; offset < allocation.size(); offset += 4096) {
    allocation[offset] = 42;
  }
  std::this_thread::sleep_for(std::chrono::seconds{1});
  return allocation.empty() ? 1 : 0;
}

int forkWorkerChild() {
  const pid_t worker = ::fork();
  if (worker < 0) {
    return 2;
  }
  if (worker == 0) {
    std::this_thread::sleep_for(std::chrono::milliseconds{250});
    _exit(0);
  }
  std::this_thread::sleep_for(std::chrono::milliseconds{150});
  int status = 0;
  return ::waitpid(worker, &status, 0) == worker && WIFEXITED(status) &&
                 WEXITSTATUS(status) == 0
             ? 0
             : 3;
}

void waitAndCleanup(CgroupV2Manager &manager, const std::string &job) {
  for (int attempt = 0; attempt < 100; ++attempt) {
    if (manager.cleanupSafe(job)) {
      manager.cleanupJob(job);
      return;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds{10});
  }
  throw std::runtime_error("integration-test cgroup did not become empty");
}
} // namespace

int main(int argc, char **argv) {
  if (argc == 3 && std::string(argv[1]) == "--allocate") {
    return allocateChild(static_cast<std::size_t>(std::stoull(argv[2])));
  }
  if (argc == 2 && std::string(argv[1]) == "--fork-worker") {
    return forkWorkerChild();
  }

  const char *configured_parent = std::getenv("SRCG_TEST_CGROUP_PARENT");
  if (!configured_parent || std::string(configured_parent).empty()) {
    std::cout << "SKIP: set SRCG_TEST_CGROUP_PARENT to an empty delegated "
                 "cgroup path\n";
    return 77;
  }

  CgroupV2Manager manager(configured_parent);
  manager.initializeParent(512ULL * 1024ULL * 1024ULL, std::uint64_t{0});

  manager.createJob("integration-inheritance", 128ULL * 1024ULL * 1024ULL,
                    std::uint64_t{0});
  ProcessSupervisor supervisor(manager, std::chrono::milliseconds{5});
  bool observed_parent_and_worker = false;
  const auto normal = supervisor.run(
      "integration-inheritance", {argv[0], "--fork-worker"},
      [&](const CgroupSnapshot &state) {
        observed_parent_and_worker =
            observed_parent_and_worker || state.processes.size() >= 2;
      });
  assert(normal.succeeded());
  assert(observed_parent_and_worker);
  waitAndCleanup(manager, "integration-inheritance");

  manager.createJob("integration-oom", 64ULL * 1024ULL * 1024ULL,
                    std::uint64_t{0});
  const auto oom = supervisor.run(
      "integration-oom",
      {argv[0], "--allocate", std::to_string(256ULL * 1024ULL * 1024ULL)});
  assert(oom.cgroup_oom);
  waitAndCleanup(manager, "integration-oom");
}
