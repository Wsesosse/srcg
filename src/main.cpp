#include "cgroup.h"
#include <chrono>
#include <iostream>
#include <thread>
#include <unistd.h>

void usage() {
  std::cout << "Usage:\n";
  std::cout << "  srcg -s <group> <memGB>\n";
  std::cout << "  srcg -g <group>\n";
}

void graph_loop(const std::string &group) {
  while (true) {
    long mb = cgroup::read_memory_mb(group);

    system("clear");
    std::cout << group << " : " << mb << " MB | ";

    int bars = mb / 50;
    for (int i = 0; i < bars; i++)
      std::cout << "#";

    std::cout << "\n";
    std::this_thread::sleep_for(std::chrono::seconds(1));
  }
}

int main(int argc, char *argv[]) {

  if (argc < 2) {
    usage();
    return 1;
  }

  std::string cmd = argv[1];

  // register shell
  if (cmd == "-s") {
    if (argc < 4) {
      usage();
      return 1;
    }

    std::string group = argv[2];
    int mem = std::stoi(argv[3]);

    cgroup::create_group(group);
    cgroup::set_memory_limit(group, mem);

    int pid = getppid();
    cgroup::add_pid(group, pid);

    std::cout << "Shell registered to " << group << "\n";
  }

  // graph
  else if (cmd == "-g") {
    if (argc < 3) {
      usage();
      return 1;
    }

    graph_loop(argv[2]);
  }

  else {
    usage();
  }

  return 0;
}