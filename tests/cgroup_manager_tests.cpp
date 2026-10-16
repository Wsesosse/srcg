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
  writeFile(temporary.path() / "memory.max", "2147483648\n");
  writeFile(temporary.path() / "memory.current", "536870912\n");
  const auto parent = temporary.path() / "srcg";
  CgroupV2Manager manager(parent, CgroupBackend::TestFilesystem);
  manager.initializeParent(1024ULL * 1024ULL * 1024ULL, std::uint64_t{0});

  // Repeating init with identical values is idempotent, but changing an
  // existing parent through init must never overwrite its limits.
  manager.initializeParent(1024ULL * 1024ULL * 1024ULL, std::uint64_t{0});
  bool overwrite_rejected = false;
  try {
    manager.initializeParent(2ULL * 1024ULL * 1024ULL * 1024ULL,
                             std::uint64_t{0});
  } catch (const std::runtime_error &) {
    overwrite_rejected = true;
  }
  assert(overwrite_rejected);
  const auto parent_state = manager.parentSnapshot();
  assert(parent_state.memory_max_bytes == 1024ULL * 1024ULL * 1024ULL);

  const auto boundaries = manager.ancestorMemoryBoundaries();
  assert(boundaries.size() == 1);
  assert(boundaries.front().path == temporary.path());
  assert(boundaries.front().limit_bytes == 2ULL * 1024ULL * 1024ULL * 1024ULL);
  assert(boundaries.front().current_bytes == 512ULL * 1024ULL * 1024ULL);
  assert(boundaries.front().available_bytes ==
         1536ULL * 1024ULL * 1024ULL);

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

  manager.setJobMemoryLimit("job-A", 768ULL * 1024ULL * 1024ULL);
  assert(manager.snapshot("job-A").memory_max_bytes ==
         768ULL * 1024ULL * 1024ULL);

  // A release must not lower memory.max beneath current usage plus margin.
  assert(!manager.lowerJobMemoryLimitIfSafe(
      "job-A", 384ULL * 1024ULL * 1024ULL,
      384ULL * 1024ULL * 1024ULL));
  assert(manager.snapshot("job-A").memory_max_bytes ==
         768ULL * 1024ULL * 1024ULL);
  assert(manager.lowerJobMemoryLimitIfSafe(
      "job-A", 512ULL * 1024ULL * 1024ULL,
      128ULL * 1024ULL * 1024ULL));
  assert(manager.snapshot("job-A").memory_max_bytes ==
         512ULL * 1024ULL * 1024ULL);

  bool parent_limit_rejected = false;
  try {
    manager.setJobMemoryLimit("job-A", 2ULL * 1024ULL * 1024ULL * 1024ULL);
  } catch (const std::invalid_argument &) {
    parent_limit_rejected = true;
  }
  assert(parent_limit_rejected);

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

  bool duplicate_active_rejected = false;
  try {
    manager.createJob("job-A", 256ULL * 1024ULL * 1024ULL,
                      std::uint64_t{0});
  } catch (const std::runtime_error &) {
    duplicate_active_rejected = true;
  }
  assert(duplicate_active_rejected);

  writeFile(job / "cgroup.events", "populated 0\n");
  writeFile(job / "cgroup.procs", "");
  // Residual charges do not make an otherwise empty cgroup active.
  writeFile(job / "memory.current", "4096\n");
  assert(manager.cleanupSafe("job-A"));

  // Reusing a logical job id automatically replaces an empty cgroup left by
  // an interrupted supervisor, even if residual memory is still charged.
  manager.createJob("job-A", 256ULL * 1024ULL * 1024ULL, std::uint64_t{0});
  const auto reused = manager.snapshot("job-A");
  assert(reused.memory_max_bytes == 256ULL * 1024ULL * 1024ULL);
  assert(reused.memory_current_bytes == 0);
  manager.cleanupJob("job-A");
  assert(!std::filesystem::exists(job));
}
