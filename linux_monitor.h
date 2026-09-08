#ifndef SRCG_LINUX_MONITOR_H
#define SRCG_LINUX_MONITOR_H

#include <cstdint>
#include <optional>
#include <string_view>
#include <sys/types.h>

struct PressureValues {
  double avg10{};
  double avg60{};
  double avg300{};
  std::uint64_t total_microseconds{};
};

struct MemoryPressureSnapshot {
  PressureValues some;
  PressureValues full;
};

struct HostMemorySnapshot {
  std::uint64_t mem_total_bytes{};
  std::uint64_t mem_available_bytes{};
  std::uint64_t swap_total_bytes{};
  std::uint64_t swap_free_bytes{};
  std::optional<MemoryPressureSnapshot> pressure;
};

struct ProcessMemorySnapshot {
  std::uint64_t vm_size_bytes{};
  std::uint64_t vm_rss_bytes{};
};

class LinuxMonitor {
public:
  [[nodiscard]] HostMemorySnapshot sampleHostMemory() const;
  [[nodiscard]] std::optional<ProcessMemorySnapshot>
  sampleProcessMemory(pid_t pid) const;

  static std::optional<MemoryPressureSnapshot>
  parseMemoryPressure(std::string_view text);
};

#endif
