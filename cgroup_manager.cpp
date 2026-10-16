#include "cgroup_manager.h"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <cstring>
#include <fstream>
#include <linux/magic.h>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/statfs.h>
#include <system_error>
#include <unistd.h>

namespace {
std::string readText(const std::filesystem::path &path) {
  std::ifstream input(path);
  if (!input) {
    throw std::runtime_error("cannot read " + path.string() + ": " +
                             std::strerror(errno));
  }
  return {std::istreambuf_iterator<char>(input),
          std::istreambuf_iterator<char>()};
}

void writeText(const std::filesystem::path &path, std::string_view value) {
  std::ofstream output(path);
  if (!output) {
    throw std::runtime_error("cannot write " + path.string() + ": " +
                             std::strerror(errno));
  }
  output << value;
  output.flush();
  if (!output) {
    throw std::runtime_error("write failed for " + path.string() + ": " +
                             std::strerror(errno));
  }
}

bool containsWord(std::string_view text, std::string_view wanted) {
  std::istringstream input{std::string(text)};
  std::string word;
  while (input >> word) {
    if (word == wanted || (word.size() == wanted.size() + 1 &&
                           (word.front() == '+' || word.front() == '-') &&
                           std::string_view(word).substr(1) == wanted)) {
      return true;
    }
  }
  return false;
}

std::uint64_t parseUnsigned(std::string_view text,
                            const std::filesystem::path &source) {
  try {
    return parseMemoryBytes(text);
  } catch (const std::exception &error) {
    throw std::runtime_error("invalid numeric value in " + source.string() +
                             ": " + error.what());
  }
}

MemoryLimit readLimit(const std::filesystem::path &path) {
  try {
    return parseMemoryLimit(readText(path));
  } catch (const std::exception &error) {
    throw std::runtime_error("invalid cgroup limit in " + path.string() + ": " +
                             error.what());
  }
}

std::filesystem::path nearestExisting(std::filesystem::path path) {
  while (!path.empty() && !std::filesystem::exists(path)) {
    const auto parent = path.parent_path();
    if (parent == path) {
      break;
    }
    path = parent;
  }
  return path;
}
} // namespace

CgroupV2Manager::CgroupV2Manager(std::filesystem::path parent,
                                 CgroupBackend backend)
    : parent_(std::move(parent)), backend_(backend) {
  if (parent_.empty()) {
    throw std::invalid_argument("cgroup parent path must not be empty");
  }
  parent_ = parent_.lexically_normal();
}

bool CgroupV2Manager::validJobId(const std::string &job_id) noexcept {
  if (job_id.empty() || job_id.size() > 64 ||
      !std::isalnum(static_cast<unsigned char>(job_id.front()))) {
    return false;
  }
  return std::all_of(job_id.begin(), job_id.end(), [](unsigned char character) {
    return std::isalnum(character) || character == '_' || character == '-' ||
           character == '.';
  });
}

MemoryEvents CgroupV2Manager::parseMemoryEvents(std::string_view text) {
  const auto fields = parseKeyValueStats(text);
  const auto get = [&fields](const char *key) {
    const auto found = fields.find(key);
    return found == fields.end() ? 0ULL : found->second;
  };
  return {.low = get("low"),
          .high = get("high"),
          .max = get("max"),
          .oom = get("oom"),
          .oom_kill = get("oom_kill"),
          .oom_group_kill = get("oom_group_kill")};
}

std::map<std::string, std::uint64_t>
CgroupV2Manager::parseKeyValueStats(std::string_view text) {
  std::map<std::string, std::uint64_t> values;
  std::istringstream input{std::string(text)};
  std::string key;
  std::uint64_t value = 0;
  while (input >> key >> value) {
    values[key] = value;
  }
  return values;
}

CgroupEnvironment CgroupV2Manager::inspectEnvironment() const {
  CgroupEnvironment result;
  result.parent = parent_;
  result.parent_exists = std::filesystem::is_directory(parent_);
  result.nearest_existing_ancestor = nearestExisting(parent_);
  if (result.nearest_existing_ancestor.empty()) {
    result.problems.emplace_back(
        "no existing ancestor for configured cgroup parent");
    return result;
  }

  if (backend_ == CgroupBackend::TestFilesystem) {
    result.unified_v2 = std::filesystem::exists(
        result.nearest_existing_ancestor / "cgroup.controllers");
  } else {
    struct statfs filesystem_info{};
    result.unified_v2 = ::statfs(result.nearest_existing_ancestor.c_str(),
                                 &filesystem_info) == 0 &&
                        static_cast<unsigned long>(filesystem_info.f_type) ==
                            CGROUP2_SUPER_MAGIC;
  }
  if (!result.unified_v2) {
    result.problems.emplace_back(
        "configured path is not on a cgroup v2 filesystem");
    return result;
  }

  const auto controllers_path =
      result.nearest_existing_ancestor / "cgroup.controllers";
  if (std::filesystem::exists(controllers_path)) {
    result.memory_controller_available =
        containsWord(readText(controllers_path), "memory");
  }
  if (!result.memory_controller_available) {
    result.problems.emplace_back("memory controller is not available at " +
                                 result.nearest_existing_ancestor.string());
  }

  if (result.parent_exists) {
    const auto subtree = parent_ / "cgroup.subtree_control";
    result.memory_controller_enabled =
        std::filesystem::exists(subtree) &&
        containsWord(readText(subtree), "memory");
    result.parent_writable = ::access(parent_.c_str(), W_OK) == 0 &&
                             ::access(subtree.c_str(), W_OK) == 0;
    if (!result.memory_controller_enabled) {
      result.problems.emplace_back(
          "memory controller is not enabled for child job cgroups");
    }
    if (!result.parent_writable) {
      result.problems.emplace_back(
          "cgroup parent is not writable by the current user");
    }
  } else {
    const auto ancestor_subtree =
        result.nearest_existing_ancestor / "cgroup.subtree_control";
    result.memory_controller_enabled =
        std::filesystem::exists(ancestor_subtree) &&
        containsWord(readText(ancestor_subtree), "memory");
    result.parent_writable =
        ::access(result.nearest_existing_ancestor.c_str(), W_OK) == 0 &&
        ::access(ancestor_subtree.c_str(), W_OK) == 0;
    result.problems.emplace_back("configured srcg parent does not exist");
    if (!result.parent_writable) {
      result.problems.emplace_back(
          "nearest cgroup ancestor is not delegated to the current user");
    }
  }
  return result;
}

void CgroupV2Manager::requireKernelCgroupV2(
    const std::filesystem::path &existing_path) const {
  if (backend_ == CgroupBackend::TestFilesystem) {
    return;
  }
  struct statfs filesystem_info{};
  if (::statfs(existing_path.c_str(), &filesystem_info) != 0 ||
      static_cast<unsigned long>(filesystem_info.f_type) !=
          CGROUP2_SUPER_MAGIC) {
    throw std::runtime_error(existing_path.string() +
                             " is not on a cgroup v2 filesystem");
  }
}

void CgroupV2Manager::ensureMemoryController(
    const std::filesystem::path &cgroup) const {
  const auto controllers = cgroup / "cgroup.controllers";
  const auto subtree = cgroup / "cgroup.subtree_control";
  if (!std::filesystem::exists(controllers) ||
      !containsWord(readText(controllers), "memory")) {
    throw std::runtime_error("memory controller is unavailable at " +
                             cgroup.string());
  }
  if (!std::filesystem::exists(subtree)) {
    if (backend_ == CgroupBackend::TestFilesystem) {
      writeText(subtree, "");
    } else {
      throw std::runtime_error("missing " + subtree.string());
    }
  }
  if (!containsWord(readText(subtree), "memory")) {
    writeText(subtree, "+memory\n");
  }
}

void CgroupV2Manager::ensureTestControlFiles(
    const std::filesystem::path &cgroup) const {
  if (backend_ != CgroupBackend::TestFilesystem) {
    return;
  }
  const std::vector<std::pair<std::string, std::string>> files{
      {"cgroup.controllers", "memory\n"},
      {"cgroup.subtree_control", ""},
      {"cgroup.procs", ""},
      {"cgroup.events", "populated 0\n"},
      {"memory.current", "0\n"},
      {"memory.peak", "0\n"},
      {"memory.max", "max\n"},
      {"memory.swap.max", "max\n"},
      {"memory.events",
       "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"},
      {"memory.stat", "anon 0\nfile 0\n"},
      {"memory.oom.group", "0\n"},
  };
  for (const auto &[name, initial] : files) {
    const auto path = cgroup / name;
    if (!std::filesystem::exists(path)) {
      writeText(path, initial);
    }
  }
}

void CgroupV2Manager::initializeParent(std::uint64_t memory_limit_bytes,
                                       MemoryLimit swap_limit_bytes) {
  if (memory_limit_bytes == 0) {
    throw std::invalid_argument("parent memory limit must be positive");
  }
  const bool parent_existed = std::filesystem::exists(parent_);
  auto ancestor = nearestExisting(parent_);
  if (ancestor.empty()) {
    throw std::runtime_error("no existing ancestor for cgroup parent " +
                             parent_.string());
  }
  requireKernelCgroupV2(ancestor);

  if (parent_existed) {
    ensureTestControlFiles(parent_);
    if (!std::filesystem::exists(parent_ / "memory.max") ||
        !std::filesystem::exists(parent_ / "memory.swap.max")) {
      throw std::runtime_error("existing parent is not a delegated memory "
                               "cgroup: " +
                               parent_.string());
    }
    const auto configured_memory = readLimit(parent_ / "memory.max");
    const auto configured_swap = readLimit(parent_ / "memory.swap.max");
    if (configured_memory != MemoryLimit{memory_limit_bytes} ||
        configured_swap != swap_limit_bytes) {
      throw std::runtime_error(
          "parent cgroup already exists with different limits; refusing to "
          "overwrite its configuration");
    }
    // Enabling the controller for child jobs is idempotent and does not alter
    // the already configured parent memory/swap limits.
    ensureMemoryController(parent_);
    return;
  }

  ensureMemoryController(ancestor);

  std::error_code error;
  if (!std::filesystem::create_directory(parent_, error)) {
    throw std::runtime_error("cannot create cgroup parent " + parent_.string() +
                             ": " + error.message());
  }
  ensureTestControlFiles(parent_);
  if (!std::filesystem::exists(parent_ / "memory.max")) {
    throw std::runtime_error("memory controller was not delegated to " +
                             parent_.string());
  }

  const auto current = parseUnsigned(readText(parent_ / "memory.current"),
                                     parent_ / "memory.current");
  if (current > memory_limit_bytes) {
    throw std::runtime_error(
        "refusing to lower parent memory.max below memory.current");
  }
  writeText(parent_ / "memory.max", std::to_string(memory_limit_bytes) + "\n");
  writeText(parent_ / "memory.swap.max",
            formatMemoryLimit(swap_limit_bytes) + "\n");
  ensureMemoryController(parent_);
}

std::filesystem::path
CgroupV2Manager::jobPath(const std::string &job_id) const {
  if (!validJobId(job_id)) {
    throw std::invalid_argument("invalid job id: " + job_id);
  }
  return parent_ / job_id;
}

std::vector<CgroupMemoryBoundary>
CgroupV2Manager::ancestorMemoryBoundaries() const {
  std::vector<CgroupMemoryBoundary> result;
  auto ancestor = parent_.parent_path();
  while (!ancestor.empty()) {
    const auto limit_path = ancestor / "memory.max";
    const auto current_path = ancestor / "memory.current";
    if (std::filesystem::exists(limit_path) &&
        std::filesystem::exists(current_path)) {
      const auto limit = readLimit(limit_path);
      if (limit) {
        const auto current = parseUnsigned(readText(current_path), current_path);
        result.push_back({.path = ancestor,
                          .limit_bytes = *limit,
                          .current_bytes = current,
                          .available_bytes =
                              current < *limit ? *limit - current : 0});
      }
    }
    const auto next = ancestor.parent_path();
    if (next == ancestor) {
      break;
    }
    ancestor = next;
  }
  return result;
}

void CgroupV2Manager::createJob(const std::string &job_id,
                                std::uint64_t memory_limit_bytes,
                                MemoryLimit swap_limit_bytes) {
  if (memory_limit_bytes == 0) {
    throw std::invalid_argument("job memory limit must be positive");
  }
  const auto path = jobPath(job_id);
  if (std::filesystem::exists(path)) {
    const auto existing = snapshotPath(path, job_id);
    if (existing.populated || !existing.processes.empty()) {
      throw std::runtime_error("job is already running: " + path.string());
    }
    // A previous supervisor may have been terminated before it could remove
    // an already empty cgroup. Reusing the logical job id must recover from
    // that stale registration without requiring a separate cleanup command.
    cleanupJob(job_id);
  }

  std::error_code error;
  if (!std::filesystem::create_directory(path, error)) {
    if (std::filesystem::exists(path)) {
      throw std::runtime_error(
          "job cgroup was created concurrently: " + path.string());
    }
    throw std::runtime_error("cannot create job cgroup " + path.string() +
                             ": " + error.message());
  }
  try {
    ensureTestControlFiles(path);
    if (!std::filesystem::exists(path / "memory.max")) {
      throw std::runtime_error("memory controller is not active for " +
                               path.string());
    }
    writeText(path / "memory.max", std::to_string(memory_limit_bytes) + "\n");
    writeText(path / "memory.swap.max",
              formatMemoryLimit(swap_limit_bytes) + "\n");
    if (std::filesystem::exists(path / "memory.oom.group")) {
      writeText(path / "memory.oom.group", "1\n");
    }
  } catch (...) {
    if (backend_ == CgroupBackend::TestFilesystem) {
      std::filesystem::remove_all(path, error);
    } else {
      ::rmdir(path.c_str());
    }
    throw;
  }
}

void CgroupV2Manager::setJobMemoryLimit(
    const std::string &job_id, std::uint64_t memory_limit_bytes) {
  if (memory_limit_bytes == 0) {
    throw std::invalid_argument("job memory limit must be positive");
  }
  const auto parent = parentSnapshot();
  if (!parent.memory_max_bytes) {
    throw std::runtime_error("srcg parent memory.max must be finite");
  }
  if (memory_limit_bytes > *parent.memory_max_bytes) {
    throw std::invalid_argument("job memory limit exceeds srcg parent limit");
  }
  const auto path = jobPath(job_id) / "memory.max";
  if (!std::filesystem::exists(path)) {
    throw std::runtime_error("job cgroup does not exist: " +
                             jobPath(job_id).string());
  }
  writeText(path, std::to_string(memory_limit_bytes) + "\n");
}

bool CgroupV2Manager::lowerJobMemoryLimitIfSafe(
    const std::string &job_id, std::uint64_t memory_limit_bytes,
    std::uint64_t safety_margin_bytes) {
  const auto state = snapshot(job_id);
  if (state.memory_max_bytes && memory_limit_bytes > *state.memory_max_bytes) {
    throw std::invalid_argument(
        "safe-lower operation cannot increase job memory limit");
  }
  if (safety_margin_bytes >
          std::numeric_limits<std::uint64_t>::max() -
              state.memory_current_bytes ||
      state.memory_current_bytes + safety_margin_bytes >
          memory_limit_bytes) {
    return false;
  }
  setJobMemoryLimit(job_id, memory_limit_bytes);
  return true;
}

void CgroupV2Manager::attachProcess(const std::string &job_id,
                                    pid_t pid) const {
  if (pid <= 0) {
    throw std::invalid_argument("cannot attach a non-positive pid");
  }
  writeText(jobPath(job_id) / "cgroup.procs", std::to_string(pid) + "\n");
}

CgroupSnapshot CgroupV2Manager::snapshotPath(const std::filesystem::path &path,
                                             std::string job_id) const {
  if (!std::filesystem::is_directory(path)) {
    throw std::runtime_error("cgroup does not exist: " + path.string());
  }
  CgroupSnapshot result;
  result.job_id = std::move(job_id);
  result.memory_current_bytes =
      parseUnsigned(readText(path / "memory.current"), path / "memory.current");
  if (std::filesystem::exists(path / "memory.peak")) {
    result.memory_peak_bytes =
        parseUnsigned(readText(path / "memory.peak"), path / "memory.peak");
  }
  result.memory_max_bytes = readLimit(path / "memory.max");
  result.swap_max_bytes = readLimit(path / "memory.swap.max");
  result.events = parseMemoryEvents(readText(path / "memory.events"));
  result.memory_stat = parseKeyValueStats(readText(path / "memory.stat"));

  const auto cgroup_events =
      parseKeyValueStats(readText(path / "cgroup.events"));
  if (const auto populated = cgroup_events.find("populated");
      populated != cgroup_events.end()) {
    result.populated = populated->second != 0;
  }
  std::istringstream process_lines(readText(path / "cgroup.procs"));
  pid_t pid = 0;
  while (process_lines >> pid) {
    result.processes.push_back(pid);
  }
  return result;
}

CgroupSnapshot CgroupV2Manager::snapshot(const std::string &job_id) const {
  return snapshotPath(jobPath(job_id), job_id);
}

CgroupSnapshot CgroupV2Manager::parentSnapshot() const {
  return snapshotPath(parent_, "srcg-parent");
}

bool CgroupV2Manager::cleanupSafe(const std::string &job_id) const {
  const auto state = snapshot(job_id);
  // memory.current is not a liveness indicator. Page cache and kernel charges
  // can remain after the last process exits; the kernel safely reparents those
  // residual charges when an empty cgroup is removed.
  return !state.populated && state.processes.empty();
}

void CgroupV2Manager::cleanupJob(const std::string &job_id) {
  const auto path = jobPath(job_id);
  if (!cleanupSafe(job_id)) {
    throw std::runtime_error("refusing to remove active cgroup " +
                             path.string());
  }
  if (backend_ == CgroupBackend::TestFilesystem) {
    std::error_code error;
    std::filesystem::remove_all(path, error);
    if (error) {
      throw std::runtime_error("cannot remove test cgroup " + path.string() +
                               ": " + error.message());
    }
    return;
  }
  if (::rmdir(path.c_str()) != 0) {
    throw std::runtime_error("cannot remove cgroup " + path.string() + ": " +
                             std::strerror(errno));
  }
}
