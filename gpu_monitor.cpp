#include "gpu_monitor.h"

#include <stdexcept>
#include <string>

namespace {
void checkNvml(nvmlReturn_t result, const char* operation) {
    if (result != NVML_SUCCESS) {
        throw std::runtime_error(std::string(operation) + ": " + nvmlErrorString(result));
    }
}
} // namespace

GPUMonitor::GPUMonitor(unsigned int device_index) {
    checkNvml(nvmlInit(), "nvmlInit");
    initialized_ = true;
    try {
        checkNvml(nvmlDeviceGetHandleByIndex(device_index, &device_), "nvmlDeviceGetHandleByIndex");
    } catch (...) {
        nvmlShutdown();
        initialized_ = false;
        throw;
    }
}

GPUMonitor::~GPUMonitor() {
    if (initialized_) {
        nvmlShutdown();
    }
}

GpuMemorySnapshot GPUMonitor::sampleMemory() const {
    nvmlMemory_t memory{};
    checkNvml(nvmlDeviceGetMemoryInfo(device_, &memory), "nvmlDeviceGetMemoryInfo");
    return {.total_bytes = memory.total, .free_bytes = memory.free, .used_bytes = memory.used};
}

PcieThroughputSnapshot GPUMonitor::samplePcieThroughput() const {
    unsigned int receive = 0;
    unsigned int transmit = 0;
    checkNvml(nvmlDeviceGetPcieThroughput(device_, NVML_PCIE_UTIL_RX_BYTES, &receive),
              "nvmlDeviceGetPcieThroughput(RX)");
    checkNvml(nvmlDeviceGetPcieThroughput(device_, NVML_PCIE_UTIL_TX_BYTES, &transmit),
              "nvmlDeviceGetPcieThroughput(TX)");
    return {.receive_kib_per_second = receive, .transmit_kib_per_second = transmit};
}
