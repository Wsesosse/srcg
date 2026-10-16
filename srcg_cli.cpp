#include "cgroup_manager.h"
#include "linux_monitor.h"
#include "memory_size.h"
#include "reservation_ipc.h"
#include "srcg_daemon.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
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
      << "  srcg init --parent-memory SIZE --host-headroom SIZE\n"
      << "            [--parent PATH] [--parent-swap SIZE]\n"
      << "  srcg serve --socket PATH --reserved-memory SIZE --host-headroom "
         "SIZE\n"
      << "             [--checkpoint-budget SIZE] [--parent PATH] [--log "
         "FILE]\n"
      << "  srcg status --job ID [--parent PATH]\n"
      << "  srcg cleanup --job ID [--parent PATH]\n"
      << "  srcg run --job ID --memory SIZE [--swap SIZE]\n"
      << "           [--parent PATH] [--interval-ms N] [--log FILE]\n"
      << "           [--checkpoint-dir PATH] [--max-restarts N]\n"
      << "           [--restart-backoff-ms N]\n"
      << "           [--manager-socket PATH]\n"
      << "           -- PROGRAM [ARGS...]\n\n"
      << "SIZE accepts bytes, KiB, MiB, GiB, KB, MB, or GB. Swap also accepts "
         "max.\n";
  std::exit(error.empty() ? 0 : 2);
}

[[noreturn]] void runUsage(const std::string &error = {}) {
  if (!error.empty()) {
    std::cerr << "srcg: " << error << "\n\n";
  }
  std::cerr
      << "Usage:\n"
      << "  srcg run --job ID --memory SIZE [--swap SIZE]\n"
      << "           [--parent PATH] [--interval-ms N] [--log FILE]\n"
      << "           [--checkpoint-dir PATH] [--max-restarts N]\n"
      << "           [--restart-backoff-ms N]\n"
      << "           [--manager-socket PATH]\n"
      << "           -- PROGRAM [ARGS...]\n\n"
      << "Options:\n"
      << "  --job ID          Logical job name; reusable after the process "
         "exits.\n"
      << "  --memory SIZE     memory.max for the complete process tree.\n"
      << "  --swap SIZE       memory.swap.max; default 0, also accepts max.\n"
      << "  --parent PATH     Parent initialized by `srcg init`.\n"
      << "  --interval-ms N   Sampling interval when --log is enabled; default "
         "500.\n"
      << "  --log FILE        Append SRCG telemetry to FILE; disabled by "
         "default.\n"
      << "  --checkpoint-dir  Expose PATH to a cooperative client and resume "
         "latest.\n"
      << "  --max-restarts N  Restart a failed job from a complete checkpoint "
         "N times.\n"
      << "  --restart-backoff-ms N  Delay between failed generation and "
         "restart.\n"
      << "  --manager-socket  Register the base quota and expose reservation "
         "IPC.\n"
      << "  --                Required separator before PROGRAM.\n\n"
      << "Lifecycle:\n"
      << "  run creates the job cgroup, launches PROGRAM inside it, waits, "
         "and\n"
      << "  removes the cgroup after the process tree becomes empty. An empty "
         "stale\n"
      << "  cgroup with the same job ID is reclaimed automatically; an active "
         "one\n"
      << "  is never replaced. SIGINT/SIGTERM/SIGHUP are forwarded to "
         "PROGRAM.\n\n"
      << "Output:\n"
      << "  PROGRAM owns terminal stdout/stderr. SRCG writes no telemetry "
         "unless\n"
      << "  --log is supplied. SRCG launch/supervision errors still use "
         "stderr.\n\n"
      << "Example:\n"
      << "  srcg run --job train-a --memory 6GiB --swap 0 -- python train.py\n";
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

void writeSnapshot(std::ostream &output, const CgroupSnapshot &state,
                   const char *event = "status") {
  output << "event=" << event << " job=" << state.job_id
         << " memory_current_bytes=" << state.memory_current_bytes
         << " memory_peak_bytes="
         << (state.memory_peak_bytes ? std::to_string(*state.memory_peak_bytes)
                                     : "unavailable")
         << " memory_max=" << formatMemoryLimit(state.memory_max_bytes)
         << " swap_max=" << formatMemoryLimit(state.swap_max_bytes)
         << " populated=" << (state.populated ? 1 : 0)
         << " process_count=" << state.processes.size()
         << " memory_event_low=" << state.events.low
         << " memory_event_high=" << state.events.high
         << " memory_event_max=" << state.events.max
         << " oom=" << state.events.oom << " oom_kill=" << state.events.oom_kill
         << '\n';
}

void writeResourceSample(std::ostream &output, const ResourceSample &sample) {
  const auto &state = sample.job;
  output << "event=memory_sample"
         << " monotonic_time_ms=" << sample.monotonic_time_ms
         << " job=" << state.job_id
         << " memory_current_bytes=" << state.memory_current_bytes
         << " memory_peak_bytes="
         << (state.memory_peak_bytes ? std::to_string(*state.memory_peak_bytes)
                                     : "unavailable")
         << " memory_max=" << formatMemoryLimit(state.memory_max_bytes)
         << " host_mem_available_bytes=" << sample.host.mem_available_bytes
         << " host_swap_free_bytes=" << sample.host.swap_free_bytes;
  if (sample.host.pressure) {
    output << " host_psi_some_avg10=" << sample.host.pressure->some.avg10
           << " host_psi_full_avg10=" << sample.host.pressure->full.avg10;
  }
  output << " memory_event_low=" << state.events.low
         << " memory_event_high=" << state.events.high
         << " memory_event_max=" << state.events.max
         << " oom=" << state.events.oom << " oom_kill=" << state.events.oom_kill
         << '\n';
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

int initCommand(int argc, char **argv) {
  std::filesystem::path parent = default_parent;
  std::uint64_t parent_memory = 0;
  MemoryLimit parent_swap = std::uint64_t{0};
  std::uint64_t host_headroom = 0;
  bool have_parent_memory = false;
  bool have_host_headroom = false;

  for (int index = 2; index < argc; ++index) {
    const std::string option = argv[index];
    if (option == "--parent") {
      parent = requireValue(index, argc, argv, option);
    } else if (option == "--parent-memory") {
      parent_memory = parseMemoryBytes(requireValue(index, argc, argv, option));
      have_parent_memory = true;
    } else if (option == "--parent-swap") {
      parent_swap = parseMemoryLimit(requireValue(index, argc, argv, option));
    } else if (option == "--host-headroom") {
      host_headroom = parseMemoryBytes(requireValue(index, argc, argv, option));
      have_host_headroom = true;
    } else {
      usage("unknown init option: " + option);
    }
  }
  if (!have_parent_memory || !have_host_headroom) {
    usage("init requires --parent-memory and --host-headroom");
  }

  CgroupV2Manager manager(parent);
  const auto host = LinuxMonitor{}.sampleHostMemory();
  auto headroom_boundary_bytes = host.mem_total_bytes;
  std::string headroom_boundary = "host";
  if (host_headroom >= host.mem_total_bytes ||
      parent_memory > host.mem_total_bytes - host_headroom) {
    throw std::runtime_error(
        "parent memory limit violates configured host headroom");
  }
  for (const auto &boundary : manager.ancestorMemoryBoundaries()) {
    if (boundary.limit_bytes < headroom_boundary_bytes) {
      headroom_boundary_bytes = boundary.limit_bytes;
      headroom_boundary = boundary.path.string();
    }
    if (host_headroom >= boundary.limit_bytes ||
        parent_memory > boundary.limit_bytes - host_headroom) {
      throw std::runtime_error(
          "parent memory limit violates configured headroom at ancestor " +
          boundary.path.string());
    }
  }

  manager.initializeParent(parent_memory, parent_swap);
  const auto configured = manager.parentSnapshot();
  std::cout << "event=parent_initialized"
            << " parent=" << parent
            << " memory_max=" << formatMemoryLimit(configured.memory_max_bytes)
            << " swap_max=" << formatMemoryLimit(configured.swap_max_bytes)
            << " host_headroom_bytes=" << host_headroom
            << " headroom_boundary=" << headroom_boundary
            << " headroom_boundary_bytes=" << headroom_boundary_bytes << '\n';
  return 0;
}

int statusCommand(int argc, char **argv) {
  const auto selection = parseJobSelection(argc, argv, 2);
  CgroupV2Manager manager(selection.parent);
  writeSnapshot(std::cout, manager.snapshot(selection.job_id));
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

int serveCommand(int argc, char **argv) {
  ReservationServiceConfig config;
  config.cgroup_parent = default_parent;
  bool have_socket = false;
  bool have_reserved_memory = false;
  bool have_host_headroom = false;
  for (int index = 2; index < argc; ++index) {
    const std::string option = argv[index];
    if (option == "--parent") {
      config.cgroup_parent = requireValue(index, argc, argv, option);
    } else if (option == "--socket") {
      config.socket_path = requireValue(index, argc, argv, option);
      have_socket = true;
    } else if (option == "--reserved-memory") {
      config.reserved_memory_capacity_bytes =
          parseMemoryBytes(requireValue(index, argc, argv, option));
      have_reserved_memory = true;
    } else if (option == "--checkpoint-budget") {
      config.checkpoint_budget_bytes =
          parseMemoryBytes(requireValue(index, argc, argv, option));
    } else if (option == "--host-headroom") {
      config.host_headroom_bytes =
          parseMemoryBytes(requireValue(index, argc, argv, option));
      have_host_headroom = true;
    } else if (option == "--log") {
      config.log_path = requireValue(index, argc, argv, option);
    } else {
      usage("unknown serve option: " + option);
    }
  }
  if (!have_socket || !have_reserved_memory || !have_host_headroom) {
    usage("serve requires --socket, --reserved-memory, and --host-headroom");
  }
  ReservationService service(std::move(config));
  service.serve();
  return 0;
}

int runCommand(int argc, char **argv) {
  DaemonConfig daemon;
  JobConfig job;
  std::optional<std::filesystem::path> log_path;
  std::optional<std::filesystem::path> checkpoint_directory;
  std::optional<std::filesystem::path> manager_socket;
  std::uint64_t max_restarts = 0;
  std::chrono::milliseconds restart_backoff{250};
  bool have_job_memory = false;
  int index = 2;
  for (; index < argc; ++index) {
    const std::string option = argv[index];
    if (option == "--help" || option == "-h") {
      runUsage();
    }
    if (option == "--") {
      ++index;
      break;
    }
    if (option == "--parent") {
      daemon.cgroup_parent = requireValue(index, argc, argv, option);
    } else if (option == "--parent-memory" || option == "--parent-swap" ||
               option == "--host-headroom") {
      runUsage(option +
               " belongs to `srcg init`; `srcg run` never modifies the "
               "parent cgroup");
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
    } else if (option == "--log") {
      log_path = requireValue(index, argc, argv, option);
    } else if (option == "--checkpoint-dir") {
      checkpoint_directory = requireValue(index, argc, argv, option);
    } else if (option == "--max-restarts") {
      max_restarts = std::stoull(requireValue(index, argc, argv, option));
    } else if (option == "--restart-backoff-ms") {
      restart_backoff = std::chrono::milliseconds{
          std::stoll(requireValue(index, argc, argv, option))};
    } else if (option == "--manager-socket") {
      manager_socket = requireValue(index, argc, argv, option);
    } else {
      if (option.starts_with("--/") || option.starts_with("--.")) {
        runUsage(
            "the `--` separator must be a separate argument before PROGRAM; "
            "write `-- " +
            option.substr(2) + "`, not `" + option + "`");
      }
      runUsage("unknown run option: " + option);
    }
  }
  if (job.job_id.empty() || !have_job_memory) {
    runUsage("run requires --job and --memory");
  }
  for (; index < argc; ++index) {
    job.command.emplace_back(argv[index]);
  }
  if (job.command.empty()) {
    runUsage("run requires a command after --");
  }
  if (max_restarts > 0 && !checkpoint_directory) {
    runUsage("--max-restarts requires --checkpoint-dir");
  }
  if (restart_backoff.count() < 0) {
    runUsage("--restart-backoff-ms cannot be negative");
  }

  if (checkpoint_directory) {
    *checkpoint_directory = std::filesystem::absolute(*checkpoint_directory);
    std::filesystem::create_directories(*checkpoint_directory);
    if (::setenv("SRCG_CHECKPOINT_DIR", checkpoint_directory->c_str(), 1) !=
            0 ||
        ::setenv("SRCG_RESUME", "auto", 1) != 0) {
      throw std::runtime_error("cannot configure checkpoint environment");
    }
  }
  if (manager_socket) {
    *manager_socket = std::filesystem::absolute(*manager_socket);
    if (::setenv("SRCG_MANAGER_SOCKET", manager_socket->c_str(), 1) != 0 ||
        ::setenv("SRCG_JOB_ID", job.job_id.c_str(), 1) != 0) {
      throw std::runtime_error(
          "cannot configure reservation client environment");
    }
  }

  SrcgDaemon srcg(daemon);
  std::ofstream log;
  SrcgDaemon::SampleCallback sample_logger;
  if (log_path) {
    const auto directory = log_path->parent_path();
    if (!directory.empty()) {
      std::filesystem::create_directories(directory);
    }
    log.open(*log_path, std::ios::out | std::ios::app);
    if (!log) {
      throw std::runtime_error("cannot open log file: " + log_path->string());
    }

    sample_logger = [&log](const ResourceSample &sample) {
      writeResourceSample(log, sample);
      log.flush();
    };
  }

  const auto parent = srcg.parentStatus();
  for (std::uint64_t generation = 0;; ++generation) {
    const auto generation_text = std::to_string(generation);
    if (::setenv("SRCG_RUN_GENERATION", generation_text.c_str(), 1) != 0) {
      throw std::runtime_error("cannot configure run generation environment");
    }
    bool registered_with_manager = false;
    if (manager_socket) {
      // Reconcile an account left by a killed supervisor. An active cgroup is
      // refused by the service and remains untouched.
      const auto reconcile =
          sendReservationRequest(*manager_socket, "RECLAIM job=" + job.job_id);
      if (!std::string_view(reconcile).starts_with("OK ") &&
          reconcile.find("reason=job_still_populated") == std::string::npos) {
        throw std::runtime_error("reservation manager reconciliation failed: " +
                                 reconcile);
      }
      const auto response = sendReservationRequest(
          *manager_socket, "REGISTER job=" + job.job_id + " base=" +
                               std::to_string(job.memory_limit_bytes));
      if (!std::string_view(response).starts_with("OK ")) {
        throw std::runtime_error("reservation manager rejected job: " +
                                 response);
      }
      registered_with_manager = true;
    }
    if (log_path) {
      log << "event=job_start job=" << job.job_id
          << " generation=" << generation
          << " memory_max=" << job.memory_limit_bytes
          << " swap_max=" << formatMemoryLimit(job.swap_limit_bytes)
          << " parent_memory_max=" << formatMemoryLimit(parent.memory_max_bytes)
          << " parent_swap_max=" << formatMemoryLimit(parent.swap_max_bytes)
          << '\n';
      log.flush();
    }

    JobRunResult outcome;
    try {
      outcome = srcg.runJob(job, sample_logger);
    } catch (...) {
      if (registered_with_manager) {
        try {
          (void)sendReservationRequest(*manager_socket,
                                       "RECLAIM job=" + job.job_id);
        } catch (...) {
        }
      }
      throw;
    }
    if (log_path) {
      writeSnapshot(log, outcome.process.final_snapshot, "job_final_memory");
      log << "event=job_exit job=" << job.job_id << " generation=" << generation
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
      log.flush();
    }

    if (registered_with_manager && outcome.cgroup_cleaned) {
      const auto response =
          sendReservationRequest(*manager_socket, "RECLAIM job=" + job.job_id);
      if (!std::string_view(response).starts_with("OK ")) {
        throw std::runtime_error("reservation manager could not reclaim job: " +
                                 response);
      }
    }

    if (outcome.process.succeeded()) {
      return 0;
    }
    const bool complete_checkpoint =
        checkpoint_directory &&
        std::filesystem::is_regular_file(*checkpoint_directory / "latest.json");
    const bool may_restart = generation < max_restarts &&
                             outcome.cgroup_cleaned && complete_checkpoint;
    if (!may_restart) {
      if (outcome.process.exit_code) {
        return *outcome.process.exit_code;
      }
      return outcome.process.termination_signal
                 ? 128 + *outcome.process.termination_signal
                 : 1;
    }
    if (log_path) {
      log << "event=job_restart job=" << job.job_id
          << " completed_generation=" << generation
          << " next_generation=" << generation + 1
          << " reason=" << classifyExit(outcome.process)
          << " backoff_ms=" << restart_backoff.count() << '\n';
      log.flush();
    }
    std::this_thread::sleep_for(restart_backoff);
  }
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
    if (command == "init") {
      return initCommand(argc, argv);
    }
    if (command == "serve") {
      return serveCommand(argc, argv);
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
    if (command == "help" && argc >= 3 && std::string_view(argv[2]) == "run") {
      runUsage();
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
