#include "linux_monitor.h"

#include <fstream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>

namespace {
std::optional<std::uint64_t> readKiBValue(const std::string &path,
                                          const std::string &wanted_key) {
  std::ifstream input(path);
  std::string line;
  while (std::getline(input, line)) {
    std::istringstream fields(line);
    std::string key;
    std::uint64_t value = 0;
    std::string unit;
    if (!(fields >> key >> value >> unit)) {
      continue;
    }
    if (key == wanted_key && unit == "kB") {
      return value * 1024ULL;
    }
  }
  return std::nullopt;
}

std::uint64_t requiredKiBValue(const std::string &path,
                               const std::string &key) {
  const auto value = readKiBValue(path, key);
  if (!value) {
    throw std::runtime_error("missing " + key + " in " + path);
  }
  return *value;
}

std::optional<PressureValues>
parsePressureLine(const std::string &line, const std::string &expected_name) {
  std::istringstream fields(line);
  std::string name;
  if (!(fields >> name) || name != expected_name) {
    return std::nullopt;
  }
  std::map<std::string, std::string> values;
  std::string field;
  while (fields >> field) {
    const auto separator = field.find('=');
    if (separator != std::string::npos) {
      values[field.substr(0, separator)] = field.substr(separator + 1);
    }
  }
  try {
    return PressureValues{.avg10 = std::stod(values.at("avg10")),
                          .avg60 = std::stod(values.at("avg60")),
                          .avg300 = std::stod(values.at("avg300")),
                          .total_microseconds =
                              std::stoull(values.at("total"))};
  } catch (const std::exception &) {
    return std::nullopt;
  }
}
} // namespace

std::optional<MemoryPressureSnapshot>
LinuxMonitor::parseMemoryPressure(std::string_view text) {
  std::istringstream input{std::string(text)};
  std::string some_line;
  std::string full_line;
  if (!std::getline(input, some_line) || !std::getline(input, full_line)) {
    return std::nullopt;
  }
  const auto some = parsePressureLine(some_line, "some");
  const auto full = parsePressureLine(full_line, "full");
  if (!some || !full) {
    return std::nullopt;
  }
  return MemoryPressureSnapshot{.some = *some, .full = *full};
}

HostMemorySnapshot LinuxMonitor::sampleHostMemory() const {
  std::optional<MemoryPressureSnapshot> pressure;
  std::ifstream pressure_file("/proc/pressure/memory");
  if (pressure_file) {
    const std::string contents{std::istreambuf_iterator<char>(pressure_file),
                               std::istreambuf_iterator<char>()};
    pressure = parseMemoryPressure(contents);
  }
  return {
      .mem_total_bytes = requiredKiBValue("/proc/meminfo", "MemTotal:"),
      .mem_available_bytes = requiredKiBValue("/proc/meminfo", "MemAvailable:"),
      .swap_total_bytes = requiredKiBValue("/proc/meminfo", "SwapTotal:"),
      .swap_free_bytes = requiredKiBValue("/proc/meminfo", "SwapFree:"),
      .pressure = pressure,
  };
}

std::optional<ProcessMemorySnapshot>
LinuxMonitor::sampleProcessMemory(pid_t pid) const {
  if (pid <= 0) {
    return std::nullopt;
  }
  const std::string path = "/proc/" + std::to_string(pid) + "/status";
  const auto vm_size = readKiBValue(path, "VmSize:");
  const auto vm_rss = readKiBValue(path, "VmRSS:");
  if (!vm_size || !vm_rss) {
    return std::nullopt;
  }
  return ProcessMemorySnapshot{.vm_size_bytes = *vm_size,
                               .vm_rss_bytes = *vm_rss};
}
