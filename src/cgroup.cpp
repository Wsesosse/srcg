#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <unistd.h>

namespace fs = std::filesystem;

const std::string ROOT = "/sys/fs/cgroup/srcg";

void usage() {
  std::cout << "Usage:\n";
  std::cout << "  srcg -s <group> <memGB>\n";
  std::cout << "  srcg -t <group>\n";
  exit(1);
}

void write_file(const std::string &path, const std::string &value) {
  std::ofstream f(path);
  if (!f) {
    std::cerr << "Error writing to " << path << "\n";
    exit(1);
  }
  f << value;
}

void ensure_root() {
  if (!fs::exists(ROOT)) {
    fs::create_directories(ROOT);
  }
}

void create_group(const std::string &group, int memGB) {
  std::string dir = ROOT + "/" + group;

  if (!fs::exists(dir)) {
    fs::create_directories(dir);

    long long bytes = static_cast<long long>(memGB) * 1024LL * 1024LL * 1024LL;

    write_file(dir + "/memory.max", std::to_string(bytes));
  }
}

void attach_shell(const std::string &group) {
  std::string path = ROOT + "/" + group + "/cgroup.procs";

  pid_t shell_pid = getppid(); // parent = shell
  write_file(path, std::to_string(shell_pid));

  std::cout << "Shell PID " << shell_pid << " attached to group '" << group
            << "'\n";
}

int main(int argc, char *argv[]) {
  if (argc < 3)
    usage();

  std::string mode = argv[1];
  std::string group = argv[2];

  ensure_root();

  if (mode == "-s") {
    if (argc != 4)
      usage();

    int memGB = std::stoi(argv[3]);
    create_group(group, memGB);
    attach_shell(group);
  } else if (mode == "-t") {
    attach_shell(group);
  } else {
    usage();
  }

  return 0;
}