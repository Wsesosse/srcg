#include "memory_size.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

int main(int argc, char **argv) {
  if (argc != 3) {
    std::cerr << "usage: srcg_memory_probe SIZE HOLD_MS\n";
    return 2;
  }

  try {
    const auto bytes = parseMemoryBytes(argv[1]);
    const auto hold_ms = std::stoull(argv[2]);
    if (bytes == 0 || bytes > std::numeric_limits<std::size_t>::max()) {
      throw std::invalid_argument("probe size does not fit this process");
    }

    std::vector<std::uint8_t> allocation(static_cast<std::size_t>(bytes));
    constexpr std::size_t page_size = 4096;
    for (std::size_t offset = 0; offset < allocation.size();
         offset += page_size) {
      allocation[offset] = static_cast<std::uint8_t>((offset / page_size) | 1U);
    }
    if (!allocation.empty()) {
      allocation.back() = 1;
    }

    std::cout << "{\"event\":\"probe_ready\",\"allocated_bytes\":" << bytes
              << ",\"hold_ms\":" << hold_ms << "}\n"
              << std::flush;
    std::this_thread::sleep_for(std::chrono::milliseconds{hold_ms});

    std::uint64_t checksum = allocation.empty() ? 0 : allocation.back();
    for (std::size_t offset = 0; offset < allocation.size();
         offset += page_size) {
      checksum += allocation[offset];
    }
    std::cout << "{\"event\":\"probe_complete\",\"allocated_bytes\":" << bytes
              << ",\"checksum\":" << checksum << "}\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "memory probe failed: " << error.what() << '\n';
    return 1;
  }
}
