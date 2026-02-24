#include <chrono>
#include <iostream>
#include <thread>
#include <vector>

int main() {
  const size_t step_mb = 100;
  const size_t step_bytes = step_mb * 1024 * 1024;

  std::vector<char *> blocks;

  auto start = std::chrono::steady_clock::now();
  size_t total_mb = 0;

  std::cout << "Starting memory allocation test...\n";

  while (true) {
    auto t1 = std::chrono::steady_clock::now();

    // allocate
    char *block = new (std::nothrow) char[step_bytes];
    if (!block) {
      std::cout << "Allocation failed at " << total_mb << " MB\n";
      break;
    }

    // touch memory (important!)
    for (size_t i = 0; i < step_bytes; i += 4096)
      block[i] = 1;

    blocks.push_back(block);
    total_mb += step_mb;

    auto t2 = std::chrono::steady_clock::now();
    auto dt =
        std::chrono::duration_cast<std::chrono::milliseconds>(t2 - t1).count();

    std::cout << "Allocated: " << total_mb << " MB | step time: " << dt
              << " ms\n";

    std::this_thread::sleep_for(std::chrono::seconds(1));
  }

  // cleanup (อาจไม่ถึง ถ้าโดน kill)
  for (auto p : blocks)
    delete[] p;

  return 0;
}