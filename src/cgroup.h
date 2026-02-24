#pragma once
#include <string>

namespace cgroup {

const std::string ROOT = "/sys/fs/cgroup/srcg";

bool exists(const std::string &path);
bool create_group(const std::string &name);
bool set_memory_limit(const std::string &name, int gb);
bool add_pid(const std::string &name, int pid);
long read_memory_mb(const std::string &name);

} // namespace cgroup