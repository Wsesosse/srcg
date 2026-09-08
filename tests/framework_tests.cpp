#include "linux_monitor.h"
#include "memory_size.h"
#include "thread_manager.h"

#include <cassert>
#include <cmath>
#include <stdexcept>
#include <unistd.h>

int main() {
  assert(parseMemoryBytes("1GiB") == 1024ULL * 1024ULL * 1024ULL);
  assert(parseMemoryBytes("2 GB") == 2ULL * 1000ULL * 1000ULL * 1000ULL);
  assert(!parseMemoryLimit("max").has_value());
  bool bad_unit_rejected = false;
  try {
    (void)parseMemoryBytes("10G");
  } catch (const std::invalid_argument &) {
    bad_unit_rejected = true;
  }
  assert(bad_unit_rejected);

  const auto pressure = LinuxMonitor::parseMemoryPressure(
      "some avg10=0.25 avg60=0.10 avg300=0.05 total=1234\n"
      "full avg10=0.02 avg60=0.01 avg300=0.00 total=99\n");
  assert(pressure.has_value());
  assert(std::abs(pressure->some.avg10 - 0.25) < 0.0001);
  assert(pressure->full.total_microseconds == 99);

  LinuxMonitor monitor;
  const auto host = monitor.sampleHostMemory();
  assert(host.mem_total_bytes > 0);
  assert(host.mem_available_bytes > 0);
  const auto process = monitor.sampleProcessMemory(getpid());
  assert(process.has_value());
  assert(process->vm_size_bytes >= process->vm_rss_bytes);

  ThreadManager pool(2);
  auto first = pool.submit([] { return 20; });
  auto second = pool.submit([](int value) { return value + 2; }, 20);
  assert(first.get() + second.get() == 42);
  pool.stop();
}
