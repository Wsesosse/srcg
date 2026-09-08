#include "cgroup_manager.h"

#include <cassert>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <unistd.h>

namespace {
class TemporaryDirectory {
public:
  TemporaryDirectory() {
    std::string pattern = "/tmp/srcg-cgroup-unit-XXXXXX";
    pattern.push_back('\0');
    path_ = ::mkdtemp(pattern.data());
    if (path_.empty()) {
      throw std::runtime_error("mkdtemp failed");
    }
  }
  ~TemporaryDirectory() { std::filesystem::remove_all(path_); }
  const std::filesystem::path &path() const { return path_; }

private:
  std::filesystem::path path_;
};

void writeFile(const std::filesystem::path &path, const std::string &value) {
  std::ofstream output(path);
  output << value;
  assert(output.good());
}
} // namespace

int main() {
  assert(CgroupV2Manager::validJobId("job-A.1"));
  assert(!CgroupV2Manager::validJobId("../escape"));
  assert(!CgroupV2Manager::validJobId("job/name"));
  assert(!CgroupV2Manager::validJobId(""));

  const auto events = CgroupV2Manager::parseMemoryEvents(
      "low 1\nhigh 2\nmax 3\noom 4\noom_kill 5\noom_group_kill 6\n");
  assert(events.high == 2);
  assert(events.oom_kill == 5);

  TemporaryDirectory temporary;
  writeFile(temporary.path() / "cgroup.controllers", "cpu memory io\n");
  writeFile(temporary.path() / "cgroup.subtree_control", "\n");
  const auto parent = temporary.path() / "srcg";
  CgroupV2Manager manager(parent, CgroupBackend::TestFilesystem);
  manager.initializeParent(1024ULL * 1024ULL * 1024ULL, std::uint64_t{0});
  manager.createJob("job-A", 512ULL * 1024ULL * 1024ULL, std::uint64_t{0});

  const auto job = parent / "job-A";
  writeFile(job / "memory.current", "1048576\n");
  writeFile(job / "memory.peak", "2097152\n");
  writeFile(job / "memory.events", "low 0\nhigh 1\nmax 2\noom 1\noom_kill 1\n");
  writeFile(job / "memory.stat", "anon 900000\nfile 148576\n");
  const auto state = manager.snapshot("job-A");
  assert(state.memory_current_bytes == 1048576);
  assert(state.memory_peak_bytes == 2097152);
  assert(state.memory_max_bytes == 512ULL * 1024ULL * 1024ULL);
  assert(state.events.oom_kill == 1);
  assert(state.memory_stat.at("anon") == 900000);

  writeFile(job / "cgroup.events", "populated 1\n");
  writeFile(job / "cgroup.procs", "1234\n");
  assert(!manager.cleanupSafe("job-A"));
  bool active_rejected = false;
  try {
    manager.cleanupJob("job-A");
  } catch (const std::runtime_error &) {
    active_rejected = true;
  }
  assert(active_rejected);

  writeFile(job / "cgroup.events", "populated 0\n");
  writeFile(job / "cgroup.procs", "");
  writeFile(job / "memory.current", "0\n");
  assert(manager.cleanupSafe("job-A"));
  manager.cleanupJob("job-A");
  assert(!std::filesystem::exists(job));
}
