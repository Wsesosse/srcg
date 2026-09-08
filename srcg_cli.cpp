#include "cgroup_manager.h"
#include "linux_monitor.h"
#include "memory_size.h"
#include "srcg_daemon.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {
constexpr const char *default_parent = "/sys/fs/cgroup/srcg";

[[noreturn]] void usage(const std::string &error = {}) {
  if (!error.empty()) {
    std::cerr << "srcg: " << error << "\n\n";
  }
  std::cerr
      << "Usage:\n"
      << "  srcg check [--parent PATH]\n"
      << "  srcg status --job ID [--parent PATH]\n"
      << "  srcg cleanup --job ID [--parent PATH]\n"
      << "  srcg run --job ID --memory SIZE --parent-memory SIZE\n"
      << "           --host-headroom SIZE [--swap SIZE] [--parent-swap SIZE]\n"
      << "           [--parent PATH] [--interval-ms N] -- PROGRAM [ARGS...]\n\n"
      << "SIZE accepts bytes, KiB, MiB, GiB, KB, MB, or GB. Swap also accepts "
         "max.\n";
  std::exit(error.empty() ? 0 : 2);
}

std::string requireValue(int &index, int argc, char **argv,
                         std::string_view option) {
  if (++index >= argc) {
    usage("missing value for " + std::string(option));
  }
  return argv[index];
}

std::filesystem::path parseParentOnly(int argc, char **argv, int start) {
  std::filesystem::path parent = default_parent;
  for (int index = start; index < argc; ++index) {
    const std::string option = argv[index];
    if (option == "--parent") {
      parent = requireValue(index, argc, argv, option);
    } else {
      usage("unknown option: " + option);
    }
  }
  return parent;
}

struct JobSelection {
  std::filesystem::path parent{default_parent};
  std::string job_id;
};

JobSelection parseJobSelection(int argc, char **argv, int start) {
  JobSelection selection;
  for (int index = start; index < argc; ++index) {
    const std::string option = argv[index];
    if (option == "--parent") {
      selection.parent = requireValue(index, argc, argv, option);
    } else if (option == "--job") {
      selection.job_id = requireValue(index, argc, argv, option);
    } else {
      usage("unknown option: " + option);
    }
  }
  if (selection.job_id.empty()) {
    usage("--job is required");
  }
  return selection;
}

void printSnapshot(const CgroupSnapshot &state, const char *event = "status") {
  std::cout << "event=" << event << " job=" << state.job_id
            << " memory_current_bytes=" << state.memory_current_bytes
            << " memory_peak_bytes="
            << (state.memory_peak_bytes
                    ? std::to_string(*state.memory_peak_bytes)
                    : "unavailable")
            << " memory_max=" << formatMemoryLimit(state.memory_max_bytes)
            << " swap_max=" << formatMemoryLimit(state.swap_max_bytes)
            << " populated=" << (state.populated ? 1 : 0)
            << " process_count=" << state.processes.size()
            << " oom=" << state.events.oom
            << " oom_kill=" << state.events.oom_kill << '\n';
}

void printResourceSample(const ResourceSample &sample) {
  const auto &state = sample.job;
  std::cout << "event=memory_sample"
            << " monotonic_time_ms=" << sample.monotonic_time_ms
            << " job=" << state.job_id
            << " memory_current_bytes=" << state.memory_current_bytes
            << " memory_peak_bytes="
            << (state.memory_peak_bytes
                    ? std::to_string(*state.memory_peak_bytes)
                    : "unavailable")
            << " memory_max=" << formatMemoryLimit(state.memory_max_bytes)
            << " host_mem_available_bytes=" << sample.host.mem_available_bytes
            << " host_swap_free_bytes=" << sample.host.swap_free_bytes;
  if (sample.host.pressure) {
    std::cout << " host_psi_some_avg10=" << sample.host.pressure->some.avg10
              << " host_psi_full_avg10=" << sample.host.pressure->full.avg10;
  }
  std::cout << " oom=" << state.events.oom
            << " oom_kill=" << state.events.oom_kill << '\n';
}

const char *classifyExit(const ProcessResult &result) {
  if (result.cgroup_oom) {
    return "cgroup_oom";
  }
  if (result.exit_code && *result.exit_code == 0) {
    return "completed";
  }
  if (result.termination_signal) {
    return "signal_exit";
  }
  return "nonzero_exit";
}

int checkCommand(int argc, char **argv) {
  const auto parent = parseParentOnly(argc, argv, 2);
  CgroupV2Manager manager(parent);
  const auto environment = manager.inspectEnvironment();
  std::cout << "parent=" << environment.parent << '\n'
            << "nearest_existing_ancestor="
            << environment.nearest_existing_ancestor << '\n'
            << "unified_v2=" << environment.unified_v2 << '\n'
            << "memory_controller_available="
            << environment.memory_controller_available << '\n'
            << "memory_controller_enabled="
            << environment.memory_controller_enabled << '\n'
            << "parent_exists=" << environment.parent_exists << '\n'
            << "parent_writable=" << environment.parent_writable << '\n';
  for (const auto &problem : environment.problems) {
    std::cout << "problem=" << problem << '\n';
  }
  return environment.ready() ? 0 : 1;
}

int statusCommand(int argc, char **argv) {
  const auto selection = parseJobSelection(argc, argv, 2);
  CgroupV2Manager manager(selection.parent);
  printSnapshot(manager.snapshot(selection.job_id));
  return 0;
}

int cleanupCommand(int argc, char **argv) {
  const auto selection = parseJobSelection(argc, argv, 2);
  CgroupV2Manager manager(selection.parent);
  manager.cleanupJob(selection.job_id);
  std::cout << "event=cgroup_cleanup job=" << selection.job_id
            << " result=removed\n";
  return 0;
}

int runCommand(int argc, char **argv) {
  DaemonConfig daemon;
  JobConfig job;
  bool have_parent_memory = false;
  bool have_host_headroom = false;
  bool have_job_memory = false;
  int index = 2;
  for (; index < argc; ++index) {
    const std::string option = argv[index];
    if (option == "--") {
      ++index;
      break;
    }
    if (option == "--parent") {
      daemon.cgroup_parent = requireValue(index, argc, argv, option);
    } else if (option == "--parent-memory") {
      daemon.parent_memory_limit_bytes =
          parseMemoryBytes(requireValue(index, argc, argv, option));
      have_parent_memory = true;
    } else if (option == "--parent-swap") {
      daemon.parent_swap_limit_bytes =
          parseMemoryLimit(requireValue(index, argc, argv, option));
    } else if (option == "--host-headroom") {
      daemon.host_headroom_bytes =
          parseMemoryBytes(requireValue(index, argc, argv, option));
      have_host_headroom = true;
    } else if (option == "--job") {
      job.job_id = requireValue(index, argc, argv, option);
    } else if (option == "--memory") {
      job.memory_limit_bytes =
          parseMemoryBytes(requireValue(index, argc, argv, option));
      have_job_memory = true;
    } else if (option == "--swap") {
      job.swap_limit_bytes =
          parseMemoryLimit(requireValue(index, argc, argv, option));
    } else if (option == "--interval-ms") {
      daemon.monitoring_interval = std::chrono::milliseconds{
          std::stoll(requireValue(index, argc, argv, option))};
    } else {
      usage("unknown run option: " + option);
    }
  }
  if (job.job_id.empty() || !have_job_memory || !have_parent_memory ||
      !have_host_headroom) {
    usage("run requires --job, --memory, --parent-memory, and --host-headroom");
  }
  for (; index < argc; ++index) {
    job.command.emplace_back(argv[index]);
  }
  if (job.command.empty()) {
    usage("run requires a command after --");
  }

  SrcgDaemon srcg(daemon);
  srcg.initialize();
  std::cout << "event=job_start job=" << job.job_id
            << " memory_max=" << job.memory_limit_bytes
            << " swap_max=" << formatMemoryLimit(job.swap_limit_bytes)
            << " parent_memory_max=" << daemon.parent_memory_limit_bytes
            << " parent_swap_max="
            << formatMemoryLimit(daemon.parent_swap_limit_bytes)
            << " host_headroom_bytes=" << daemon.host_headroom_bytes << '\n';
  const auto outcome = srcg.runJob(job, printResourceSample);
  printSnapshot(outcome.process.final_snapshot, "job_final_memory");
  std::cout << "event=job_exit job=" << job.job_id
            << " pid=" << outcome.process.pid
            << " classification=" << classifyExit(outcome.process)
            << " exit_code="
            << (outcome.process.exit_code
                    ? std::to_string(*outcome.process.exit_code)
                    : "none")
            << " signal="
            << (outcome.process.termination_signal
                    ? std::to_string(*outcome.process.termination_signal)
                    : "none")
            << " cgroup_cleaned=" << outcome.cgroup_cleaned << '\n';

  if (outcome.process.exit_code) {
    return *outcome.process.exit_code;
  }
  return outcome.process.termination_signal
             ? 128 + *outcome.process.termination_signal
             : 1;
}
} // namespace

int main(int argc, char **argv) {
  try {
    if (argc < 2) {
      usage("missing command");
    }
    const std::string command = argv[1];
    if (command == "check") {
      return checkCommand(argc, argv);
    }
    if (command == "status") {
      return statusCommand(argc, argv);
    }
    if (command == "cleanup") {
      return cleanupCommand(argc, argv);
    }
    if (command == "run") {
      return runCommand(argc, argv);
    }
    if (command == "help" || command == "--help" || command == "-h") {
      usage();
    }
    usage("unknown command: " + command);
  } catch (const std::exception &error) {
    std::cerr << "srcg: " << error.what() << '\n';
    return 1;
  }
}
