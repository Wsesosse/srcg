#include "memory_size.h"

#include <algorithm>
#include <cctype>
#include <charconv>
#include <limits>
#include <stdexcept>

namespace {
std::string trim(std::string_view input) {
  const auto first = input.find_first_not_of(" \t\r\n");
  if (first == std::string_view::npos) {
    return {};
  }
  const auto last = input.find_last_not_of(" \t\r\n");
  return std::string(input.substr(first, last - first + 1));
}

std::uint64_t multiplierFor(std::string suffix) {
  suffix.erase(std::remove_if(suffix.begin(), suffix.end(),
                              [](unsigned char character) {
                                return std::isspace(character);
                              }),
               suffix.end());
  std::transform(suffix.begin(), suffix.end(), suffix.begin(),
                 [](unsigned char character) {
                   return static_cast<char>(std::tolower(character));
                 });
  if (suffix.empty() || suffix == "b") {
    return 1;
  }
  if (suffix == "kib") {
    return 1024ULL;
  }
  if (suffix == "mib") {
    return 1024ULL * 1024ULL;
  }
  if (suffix == "gib") {
    return 1024ULL * 1024ULL * 1024ULL;
  }
  if (suffix == "kb") {
    return 1000ULL;
  }
  if (suffix == "mb") {
    return 1000ULL * 1000ULL;
  }
  if (suffix == "gb") {
    return 1000ULL * 1000ULL * 1000ULL;
  }
  throw std::invalid_argument("unsupported memory unit: " + suffix);
}
} // namespace

MemoryLimit parseMemoryLimit(std::string_view text) {
  const auto value = trim(text);
  if (value == "max") {
    return std::nullopt;
  }
  return parseMemoryBytes(value);
}

std::uint64_t parseMemoryBytes(std::string_view text) {
  const auto value = trim(text);
  if (value.empty()) {
    throw std::invalid_argument("memory size is empty");
  }

  std::size_t digits = 0;
  while (digits < value.size() &&
         std::isdigit(static_cast<unsigned char>(value[digits]))) {
    ++digits;
  }
  if (digits == 0) {
    throw std::invalid_argument("memory size must begin with an integer: " +
                                value);
  }

  std::uint64_t amount = 0;
  const auto result =
      std::from_chars(value.data(), value.data() + digits, amount);
  if (result.ec != std::errc{} || result.ptr != value.data() + digits) {
    throw std::invalid_argument("invalid memory size: " + value);
  }
  const auto multiplier = multiplierFor(value.substr(digits));
  if (amount > std::numeric_limits<std::uint64_t>::max() / multiplier) {
    throw std::overflow_error("memory size is too large: " + value);
  }
  return amount * multiplier;
}

std::string formatMemoryLimit(MemoryLimit limit) {
  return limit ? std::to_string(*limit) : "max";
}
