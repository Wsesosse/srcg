#include "cgroup.h"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <unistd.h>

namespace fs = std::filesystem;

namespace cgroup {

bool exists(const std::string &path) { return fs::exists(path); }

bool create_group(const std::string &name) {
  std::string dir = ROOT + "/" + name;

  if (!exists(ROOT)) {
    fs::create_directory(ROOT);
  }

  if (!exists(dir)) {
    return fs::create_directory(dir);
  }

  return true;
}

bool set_memory_limit(const std::string &name, int gb) {
  std::string file = ROOT + "/" + name + "/memory.max";
  long bytes = (long)gb * 1024 * 1024 * 1024;

  std::ofstream ofs(file);
  if (!ofs)
    return false;

  ofs << bytes;
  return true;
}

bool add_pid(const std::string &name, int pid) {
  std::string file = ROOT + "/" + name + "/cgroup.procs";

  std::ofstream ofs(file);
  if (!ofs)
    return false;

  ofs << pid;
  return true;
}

long read_memory_mb(const std::string &name) {
  std::string file = ROOT + "/" + name + "/memory.current";
  std::ifstream ifs(file);

  if (!ifs)
    return -1;

  long bytes;
  ifs >> bytes;

  return bytes / 1024 / 1024;
}

} // namespace cgroup