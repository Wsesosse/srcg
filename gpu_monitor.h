#ifndef SRCG_GPU_MONITOR_H
#define SRCG_GPU_MONITOR_H

#include <cstdint>
#include <nvml.h>

struct GpuMemorySnapshot {
    std::uint64_t total_bytes{};
    std::uint64_t free_bytes{};
    std::uint64_t used_bytes{};
};

struct PcieThroughputSnapshot {
    std::uint32_t receive_kib_per_second{};
    std::uint32_t transmit_kib_per_second{};
};

class GPUMonitor {
public:
    explicit GPUMonitor(unsigned int device_index = 0);
    ~GPUMonitor();

    GPUMonitor(const GPUMonitor&) = delete;
    GPUMonitor& operator=(const GPUMonitor&) = delete;

    [[nodiscard]] GpuMemorySnapshot sampleMemory() const;
    [[nodiscard]] PcieThroughputSnapshot samplePcieThroughput() const;

private:
    nvmlDevice_t device_{};
    bool initialized_{false};
};

#endif
