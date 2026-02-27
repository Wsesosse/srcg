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
  std::cout << "  srcg -d <group>\n";
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
  // Enable memory controller for child cgroups
  write_file(ROOT + "/cgroup.subtree_control", "+memory");
}

void create_group(const std::string &group, int memGB) {
  std::string dir = ROOT + "/" + group;

  if (!fs::exists(dir)) {
    fs::create_directories(dir);

    long long bytes = static_cast<long long>(memGB) * 1024LL * 1024LL * 1024LL;

    write_file(dir + "/memory.max", std::to_string(bytes));
  }
}

void destruct_group(const std::string &group) {
  std::string dir = ROOT + "/" + group;

  if (fs::exists(dir)) {
    fs::remove_all(dir);
  }
}

pid_t get_shell_pid() {
  // When run via sudo: shell → sudo → srcg
  // getppid() gives sudo's PID, we need the shell (grandparent)
  pid_t parent = getppid();
  std::string status_path = "/proc/" + std::to_string(parent) + "/status";
  std::ifstream status(status_path);
  std::string line;
  while (std::getline(status, line)) {
    if (line.substr(0, 5) == "PPid:") {
      return std::stoi(line.substr(6));
    }
  }
  return parent; // fallback
}

void attach_shell(const std::string &group) {
  std::string path = ROOT + "/" + group + "/cgroup.procs";

  pid_t shell_pid = get_shell_pid();
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
  } else if (mode == "-d") {
    destruct_group(group);
  } else {
    usage();
  }

  return 0;
}