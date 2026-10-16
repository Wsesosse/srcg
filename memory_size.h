#ifndef SRCG_MEMORY_SIZE_H
#define SRCG_MEMORY_SIZE_H

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

// nullopt represents the cgroup value "max".
using MemoryLimit = std::optional<std::uint64_t>;

[[nodiscard]] MemoryLimit parseMemoryLimit(std::string_view text);
[[nodiscard]] std::uint64_t parseMemoryBytes(std::string_view text);
[[nodiscard]] std::string formatMemoryLimit(MemoryLimit limit);

#endif
