#ifndef SRCG_CGROUP_MANAGER_H
#define SRCG_CGROUP_MANAGER_H

#include "memory_size.h"

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <sys/types.h>
#include <vector>

enum class CgroupBackend {
  Kernel,
  TestFilesystem,
};

struct CgroupEnvironment {
  std::filesystem::path parent;
  std::filesystem::path nearest_existing_ancestor;
  bool unified_v2{false};
  bool memory_controller_available{false};
  bool memory_controller_enabled{false};
  bool parent_exists{false};
  bool parent_writable{false};
  std::vector<std::string> problems;

  [[nodiscard]] bool ready() const noexcept {
    return unified_v2 && memory_controller_available && parent_exists &&
           memory_controller_enabled && parent_writable && problems.empty();
  }
};

struct MemoryEvents {
  std::uint64_t low{};
  std::uint64_t high{};
  std::uint64_t max{};
  std::uint64_t oom{};
  std::uint64_t oom_kill{};
  std::uint64_t oom_group_kill{};
};

struct CgroupSnapshot {
  std::string job_id;
  std::uint64_t memory_current_bytes{};
  std::optional<std::uint64_t> memory_peak_bytes;
  MemoryLimit memory_max_bytes;
  MemoryLimit swap_max_bytes;
  MemoryEvents events;
  std::map<std::string, std::uint64_t> memory_stat;
  bool populated{false};
  std::vector<pid_t> processes;
};

class CgroupV2Manager {
public:
  explicit CgroupV2Manager(std::filesystem::path parent,
                           CgroupBackend backend = CgroupBackend::Kernel);

  [[nodiscard]] CgroupEnvironment inspectEnvironment() const;
  void initializeParent(std::uint64_t memory_limit_bytes,
                        MemoryLimit swap_limit_bytes);
  void createJob(const std::string &job_id, std::uint64_t memory_limit_bytes,
                 MemoryLimit swap_limit_bytes);
  void attachProcess(const std::string &job_id, pid_t pid) const;
  [[nodiscard]] CgroupSnapshot snapshot(const std::string &job_id) const;
  [[nodiscard]] bool cleanupSafe(const std::string &job_id) const;
  void cleanupJob(const std::string &job_id);

  [[nodiscard]] const std::filesystem::path &parentPath() const noexcept {
    return parent_;
  }
  [[nodiscard]] CgroupBackend backend() const noexcept { return backend_; }

  static bool validJobId(const std::string &job_id) noexcept;
  static MemoryEvents parseMemoryEvents(std::string_view text);
  static std::map<std::string, std::uint64_t>
  parseKeyValueStats(std::string_view text);

private:
  [[nodiscard]] std::filesystem::path jobPath(const std::string &job_id) const;
  [[nodiscard]] CgroupSnapshot snapshotPath(const std::filesystem::path &path,
                                            std::string job_id) const;
  void requireKernelCgroupV2(const std::filesystem::path &existing_path) const;
  void ensureMemoryController(const std::filesystem::path &cgroup) const;
  void ensureTestControlFiles(const std::filesystem::path &cgroup) const;

  std::filesystem::path parent_;
  CgroupBackend backend_;
};

#endif
