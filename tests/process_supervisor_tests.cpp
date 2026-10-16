#include "cgroup_manager.h"
#include "process_supervisor.h"

#include <cassert>
#include <filesystem>
#include <fstream>
#include <memory>
#include <signal.h>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>

namespace {
class Fixture {
public:
  Fixture() {
    std::string pattern = "/tmp/srcg-process-unit-XXXXXX";
    pattern.push_back('\0');
    root_ = ::mkdtemp(pattern.data());
    if (root_.empty()) {
      throw std::runtime_error("mkdtemp failed");
    }
    write(root_ / "cgroup.controllers", "memory\n");
    write(root_ / "cgroup.subtree_control", "\n");
    manager_ = std::make_unique<CgroupV2Manager>(root_ / "srcg",
                                                 CgroupBackend::TestFilesystem);
    manager_->initializeParent(1024ULL * 1024ULL * 1024ULL, std::uint64_t{0});
  }
  ~Fixture() { std::filesystem::remove_all(root_); }

  CgroupV2Manager &manager() { return *manager_; }
  std::filesystem::path jobPath(const std::string &job) const {
    return root_ / "srcg" / job;
  }

  static void write(const std::filesystem::path &path,
                    const std::string &value) {
    std::ofstream output(path);
    output << value;
    assert(output.good());
  }

private:
  std::filesystem::path root_;
  std::unique_ptr<CgroupV2Manager> manager_;
};
} // namespace

int main() {
  Fixture fixture;
  fixture.manager().createJob("exit-test", 256ULL * 1024ULL * 1024ULL,
                              std::uint64_t{0});
  ProcessSupervisor supervisor(fixture.manager(), std::chrono::milliseconds{5});
  const auto exited = supervisor.run("exit-test", {"/bin/sh", "-c", "exit 7"});
  assert(exited.exit_code == 7);
  assert(!exited.termination_signal.has_value());
  assert(!exited.cgroup_oom);

  fixture.manager().createJob("exec-failure-test", 256ULL * 1024ULL * 1024ULL,
                              std::uint64_t{0});
  const auto exec_failure =
      supervisor.run("exec-failure-test", {"/srcg/not-a-real-program"});
  assert(exec_failure.exit_code == 127);
  assert(!exec_failure.cgroup_oom);

  fixture.manager().createJob("oom-event-test", 256ULL * 1024ULL * 1024ULL,
                              std::uint64_t{0});
  bool injected = false;
  const auto classified = supervisor.run(
      "oom-event-test", {"/bin/sleep", "0.05"}, [&](const CgroupSnapshot &) {
        if (!injected) {
          Fixture::write(fixture.jobPath("oom-event-test") / "memory.events",
                         "low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n");
          injected = true;
        }
      });
  assert(classified.exit_code == 0);
  assert(classified.cgroup_oom);

  fixture.manager().createJob("signal-forward-test",
                              256ULL * 1024ULL * 1024ULL, std::uint64_t{0});
  std::thread terminator([] {
    std::this_thread::sleep_for(std::chrono::milliseconds{50});
    ::kill(::getpid(), SIGTERM);
  });
  const auto terminated =
      supervisor.run("signal-forward-test", {"/bin/sleep", "10"});
  terminator.join();
  assert(!terminated.exit_code.has_value());
  assert(terminated.termination_signal == SIGTERM);
}
