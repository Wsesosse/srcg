#include "cgroup_manager.h"
#include "reservation_ipc.h"

#include <cassert>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>

namespace {
void writeFile(const std::filesystem::path &path, const std::string &value) {
  std::ofstream output(path);
  output << value;
}

std::string readFile(const std::filesystem::path &path) {
  std::ifstream input(path);
  return {std::istreambuf_iterator<char>(input),
          std::istreambuf_iterator<char>()};
}
} // namespace

int main() {
  const auto root = std::filesystem::temp_directory_path() /
                    ("srcg-ipc-test-" + std::to_string(::getpid()));
  const auto parent = root / "srcg";
  const auto socket = root / "manager.sock";
  std::filesystem::create_directories(root);

  // Some CI sandboxes deny AF_UNIX bind even below /tmp. Treat that as an
  // unavailable integration facility, not as a protocol failure.
  const auto probe_path = root / "probe.sock";
  const int probe = ::socket(AF_UNIX, SOCK_STREAM, 0);
  assert(probe >= 0);
  sockaddr_un probe_address{};
  probe_address.sun_family = AF_UNIX;
  const auto probe_text = probe_path.string();
  assert(probe_text.size() < sizeof(probe_address.sun_path));
  std::memcpy(probe_address.sun_path, probe_text.c_str(),
              probe_text.size() + 1);
  if (::bind(probe, reinterpret_cast<const sockaddr *>(&probe_address),
             sizeof(probe_address)) != 0 &&
      errno == EPERM) {
    ::close(probe);
    std::filesystem::remove_all(root);
    return 77;
  }
  ::close(probe);
  std::filesystem::remove(probe_path);
  writeFile(root / "cgroup.controllers", "memory\n");
  writeFile(root / "cgroup.subtree_control", "memory\n");
  writeFile(root / "memory.max", "1073741824\n");
  writeFile(root / "memory.current", "0\n");

  CgroupV2Manager setup(parent, CgroupBackend::TestFilesystem);
  setup.initializeParent(1024 * 1024 * 1024ULL, std::uint64_t{0});
  setup.createJob("job", 256 * 1024 * 1024ULL, std::uint64_t{0});

  bool unsafe_parent_rejected = false;
  try {
    ReservationService unsafe(
        {.cgroup_parent = parent,
         .socket_path = root / "unsafe.sock",
         .reserved_memory_capacity_bytes = 256 * 1024 * 1024ULL,
         .checkpoint_budget_bytes = 0,
         .host_headroom_bytes = 1,
         .verify_peer_cgroup = false},
        CgroupBackend::TestFilesystem);
  } catch (const std::runtime_error &) {
    unsafe_parent_rejected = true;
  }
  assert(unsafe_parent_rejected);

  ReservationService service(
      {.cgroup_parent = parent,
       .socket_path = socket,
       .reserved_memory_capacity_bytes = 256 * 1024 * 1024ULL,
       .checkpoint_budget_bytes = 128 * 1024 * 1024ULL,
       .host_headroom_bytes = 0,
       .verify_peer_cgroup = false},
      CgroupBackend::TestFilesystem);
  std::thread server([&service] { service.serve(); });
  for (int attempt = 0; attempt < 100 && !std::filesystem::exists(socket);
       ++attempt) {
    std::this_thread::sleep_for(std::chrono::milliseconds{5});
  }
  assert(std::filesystem::exists(socket));
  assert(sendReservationRequest(socket, "PING") == "OK state=ready");

  // A second service must refuse the live socket without unlinking the first
  // service's pathname.
  bool duplicate_rejected = false;
  try {
    ReservationService duplicate(
        {.cgroup_parent = parent,
         .socket_path = socket,
         .reserved_memory_capacity_bytes = 256 * 1024 * 1024ULL,
         .checkpoint_budget_bytes = 128 * 1024 * 1024ULL,
         .host_headroom_bytes = 0,
         .verify_peer_cgroup = false},
        CgroupBackend::TestFilesystem);
    duplicate.serve();
  } catch (const std::runtime_error &) {
    duplicate_rejected = true;
  }
  assert(duplicate_rejected);
  assert(sendReservationRequest(socket, "PING") == "OK state=ready");

  assert(sendReservationRequest(socket, "REGISTER job=job base=268435456") ==
         "OK state=registered");

  // Model a job already using its complete base quota so the first estimate
  // has no existing headroom and must borrow the full 64 MiB.
  writeFile(parent / "job" / "memory.current", "268435456\n");
  const auto granted = sendReservationRequest(
      socket, "RESERVE job=job request=batch-1 bytes=67108864 safety=0");
  assert(granted.starts_with("GRANTED "));
  assert(granted.find("borrowed_bytes=67108864") != std::string::npos);
  assert(readFile(parent / "job" / "memory.max") == "335544320\n");

  const auto token_start = granted.find("token=");
  assert(token_start != std::string::npos);
  const auto token = granted.substr(token_start + 6);
  const auto shrunk = sendReservationRequest(
      socket, "SHRINK job=job token=" + token + " remaining=0 safety=0");
  assert(shrunk.starts_with("SHRUNK "));
  assert(shrunk.find("returned_bytes=67108864") != std::string::npos);
  assert(readFile(parent / "job" / "memory.max") == "268435456\n");
  const auto released = sendReservationRequest(
      socket, "RELEASE job=job token=" + token + " safety=0");
  assert(released.starts_with("RELEASED "));
  assert(readFile(parent / "job" / "memory.max") == "268435456\n");

  const auto checkpoint_begin = sendReservationRequest(
      socket, "CHECKPOINT_BEGIN job=job request=save-1 safety=0");
  assert(checkpoint_begin.starts_with("GRANTED "));
  assert(readFile(parent / "job" / "memory.max") == "402653184\n");
  const auto checkpoint_token =
      checkpoint_begin.substr(checkpoint_begin.find("token=") + 6);
  writeFile(parent / "job" / "memory.current", "335544320\n");
  const auto checkpoint_pending = sendReservationRequest(
      socket, "CHECKPOINT_END job=job token=" + checkpoint_token + " safety=0");
  assert(checkpoint_pending.starts_with("PENDING "));
  assert(readFile(parent / "job" / "memory.max") == "402653184\n");
  const auto checkpoint_reuse = sendReservationRequest(
      socket, "CHECKPOINT_BEGIN job=job request=save-2 safety=0");
  assert(checkpoint_reuse.starts_with("GRANTED "));
  const auto second_checkpoint_token =
      checkpoint_reuse.substr(checkpoint_reuse.find("token=") + 6);
  assert(second_checkpoint_token != checkpoint_token);
  assert(sendReservationRequest(
             socket, "CHECKPOINT_END job=job token=" + checkpoint_token +
                         " safety=0")
             .starts_with("ALREADY_RELEASED "));
  writeFile(parent / "job" / "memory.current", "268435456\n");
  const auto checkpoint_end = sendReservationRequest(
      socket, "CHECKPOINT_END job=job token=" + second_checkpoint_token +
                  " safety=0");
  assert(checkpoint_end.starts_with("RELEASED "));
  assert(readFile(parent / "job" / "memory.max") == "268435456\n");

  const auto holding = sendReservationRequest(
      socket,
      "RESERVE job=job request=batch-holding bytes=134217728 safety=0");
  assert(holding.starts_with("GRANTED "));
  const auto holding_token_start = holding.find("token=");
  assert(holding_token_start != std::string::npos);
  const auto holding_token = holding.substr(holding_token_start + 6);
  writeFile(parent / "job" / "memory.current", "402653184\n");
  const auto wait = sendReservationRequest(
      socket, "RESERVE job=job request=batch-wait bytes=201326592 safety=0");
  assert(wait.starts_with("WAIT "));
  assert(sendReservationRequest(socket, "CANCEL job=job request=batch-wait") ==
         "OK cancelled=1");
  assert(sendReservationRequest(socket, "CANCEL job=job request=batch-wait") ==
         "OK cancelled=0");
  writeFile(parent / "job" / "memory.current", "268435456\n");
  const auto holding_released = sendReservationRequest(
      socket, "RELEASE job=job token=" + holding_token + " safety=0");
  assert(holding_released.starts_with("RELEASED "));

  // The host may have plenty of RAM while the service is inside a tighter
  // container/systemd cgroup. Admission must use the tighter ancestor slack.
  writeFile(root / "memory.current", "1048576000\n");
  const auto ancestor_wait = sendReservationRequest(
      socket,
      "RESERVE job=job request=batch-ancestor bytes=67108864 safety=0");
  assert(ancestor_wait.starts_with("WAIT "));
  assert(ancestor_wait.find("reason=host_headroom") != std::string::npos);
  assert(sendReservationRequest(socket,
                                "CANCEL job=job request=batch-ancestor") ==
         "OK cancelled=1");

  assert(sendReservationRequest(socket, "STOP") == "OK state=stopping");
  server.join();
  std::filesystem::remove_all(root);
}
