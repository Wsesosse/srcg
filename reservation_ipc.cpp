#include "reservation_ipc.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstring>
#include <fcntl.h>
#include <map>
#include <poll.h>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#include <utility>

namespace {
volatile std::sig_atomic_t service_stop_requested = 0;

void requestServiceStop(int) noexcept { service_stop_requested = 1; }

class ServiceSignalScope {
public:
  ServiceSignalScope() {
    struct sigaction action{};
    action.sa_handler = requestServiceStop;
    ::sigemptyset(&action.sa_mask);
    for (std::size_t index = 0; index < signals_.size(); ++index) {
      if (::sigaction(signals_[index], &action, &previous_[index]) != 0) {
        installed_ = index;
        restore();
        throw std::runtime_error("cannot install service signal handler");
      }
      installed_ = index + 1;
    }
    service_stop_requested = 0;
  }
  ~ServiceSignalScope() { restore(); }

private:
  void restore() noexcept {
    for (std::size_t index = 0; index < installed_; ++index) {
      ::sigaction(signals_[index], &previous_[index], nullptr);
    }
    installed_ = 0;
  }
  static constexpr std::array<int, 2> signals_{SIGINT, SIGTERM};
  std::array<struct sigaction, signals_.size()> previous_{};
  std::size_t installed_{};
};

void closeNoInterrupt(int descriptor) noexcept {
  if (descriptor < 0) {
    return;
  }
  while (::close(descriptor) != 0 && errno == EINTR) {
  }
}

std::uint64_t parseUnsigned(const std::string &value, std::string_view field) {
  std::size_t consumed = 0;
  const auto parsed = std::stoull(value, &consumed);
  if (consumed != value.size()) {
    throw std::invalid_argument("invalid " + std::string(field));
  }
  return parsed;
}

struct ParsedRequest {
  std::string operation;
  std::map<std::string, std::string> fields;
};

ParsedRequest parseRequest(const std::string &request) {
  std::istringstream input(request);
  ParsedRequest parsed;
  input >> parsed.operation;
  if (parsed.operation.empty()) {
    throw std::invalid_argument("empty request");
  }
  std::string item;
  while (input >> item) {
    const auto separator = item.find('=');
    if (separator == std::string::npos || separator == 0 ||
        separator + 1 == item.size()) {
      throw std::invalid_argument("invalid request field");
    }
    if (!parsed.fields
             .emplace(item.substr(0, separator), item.substr(separator + 1))
             .second) {
      throw std::invalid_argument("duplicate request field");
    }
  }
  return parsed;
}

std::string required(const ParsedRequest &request, const std::string &field) {
  const auto found = request.fields.find(field);
  if (found == request.fields.end()) {
    throw std::invalid_argument("missing " + field);
  }
  return found->second;
}

const char *reserveName(ReserveDecision decision) {
  switch (decision) {
  case ReserveDecision::Granted:
    return "GRANTED";
  case ReserveDecision::Wait:
    return "WAIT";
  case ReserveDecision::Denied:
    return "DENIED";
  }
  return "DENIED";
}

const char *releaseName(ReleaseDecision decision) {
  switch (decision) {
  case ReleaseDecision::Released:
    return "RELEASED";
  case ReleaseDecision::Pending:
    return "PENDING";
  case ReleaseDecision::AlreadyReleased:
    return "ALREADY_RELEASED";
  case ReleaseDecision::Denied:
    return "DENIED";
  }
  return "DENIED";
}

const char *shrinkName(ShrinkDecision decision) {
  switch (decision) {
  case ShrinkDecision::Shrunk:
    return "SHRUNK";
  case ShrinkDecision::Unchanged:
    return "UNCHANGED";
  case ShrinkDecision::AlreadyReleased:
    return "ALREADY_RELEASED";
  case ShrinkDecision::Denied:
    return "DENIED";
  }
  return "DENIED";
}

sockaddr_un socketAddress(const std::filesystem::path &path) {
  const auto value = path.string();
  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  if (value.size() >= sizeof(address.sun_path)) {
    throw std::invalid_argument("Unix socket path is too long: " + value);
  }
  std::memcpy(address.sun_path, value.c_str(), value.size() + 1);
  return address;
}

void writeAll(int descriptor, std::string_view data) {
  while (!data.empty()) {
    const auto written =
        ::send(descriptor, data.data(), data.size(), MSG_NOSIGNAL);
    if (written < 0 && errno == EINTR) {
      continue;
    }
    if (written <= 0) {
      throw std::runtime_error("Unix socket write failed: " +
                               std::string(std::strerror(errno)));
    }
    data.remove_prefix(static_cast<std::size_t>(written));
  }
}

std::string readLine(int descriptor) {
  std::string result;
  result.reserve(512);
  char buffer[512];
  while (result.size() <= 8192) {
    const auto count = ::read(descriptor, buffer, sizeof(buffer));
    if (count < 0 && errno == EINTR) {
      continue;
    }
    if (count < 0) {
      throw std::runtime_error("Unix socket read failed: " +
                               std::string(std::strerror(errno)));
    }
    if (count == 0) {
      break;
    }
    result.append(buffer, static_cast<std::size_t>(count));
    if (const auto newline = result.find('\n'); newline != std::string::npos) {
      result.resize(newline);
      return result;
    }
  }
  if (result.size() > 8192) {
    throw std::runtime_error("Unix socket message exceeds 8192 bytes");
  }
  return result;
}
} // namespace

ReservationService::ReservationService(ReservationServiceConfig config,
                                       CgroupBackend backend)
    : config_(std::move(config)), cgroups_(config_.cgroup_parent, backend) {
  const auto parent = cgroups_.parentSnapshot();
  if (!parent.memory_max_bytes) {
    throw std::runtime_error(
        "reservation service requires finite parent memory.max");
  }
  for (const auto &boundary : cgroups_.ancestorMemoryBoundaries()) {
    if (config_.host_headroom_bytes >= boundary.limit_bytes ||
        *parent.memory_max_bytes >
            boundary.limit_bytes - config_.host_headroom_bytes) {
      throw std::runtime_error(
          "srcg parent limit violates configured headroom at ancestor " +
          boundary.path.string());
    }
  }
  reservations_ = std::make_unique<ReservationManager>(
      ReservationManagerConfig{
          .parent_limit_bytes = *parent.memory_max_bytes,
          .reserved_memory_capacity_bytes =
              config_.reserved_memory_capacity_bytes,
          .checkpoint_budget_bytes = config_.checkpoint_budget_bytes,
          .host_headroom_bytes = config_.host_headroom_bytes,
      },
      [this](const std::string &job_id, std::uint64_t limit) {
        cgroups_.setJobMemoryLimit(job_id, limit);
      });
  if (!config_.log_path.empty()) {
    const auto directory = config_.log_path.parent_path();
    if (!directory.empty()) {
      std::filesystem::create_directories(directory);
    }
    log_.open(config_.log_path, std::ios::out | std::ios::app);
    if (!log_) {
      throw std::runtime_error("cannot open reservation log: " +
                               config_.log_path.string());
    }
  }
}

ReservationService::~ReservationService() {
  closeNoInterrupt(listener_);
  if (owns_socket_ && !config_.socket_path.empty()) {
    std::error_code ignored;
    std::filesystem::remove(config_.socket_path, ignored);
  }
}

void ReservationService::serve() {
  const auto directory = config_.socket_path.parent_path();
  if (!directory.empty()) {
    std::filesystem::create_directories(directory);
  }
  if (std::filesystem::exists(config_.socket_path)) {
    struct stat info{};
    if (::lstat(config_.socket_path.c_str(), &info) != 0 ||
        !S_ISSOCK(info.st_mode)) {
      throw std::runtime_error("refusing to replace non-socket path: " +
                               config_.socket_path.string());
    }
    try {
      (void)sendReservationRequest(config_.socket_path, "PING");
      throw std::runtime_error("reservation socket is already active: " +
                               config_.socket_path.string());
    } catch (const std::runtime_error &error) {
      if (std::string_view(error.what())
              .starts_with("reservation socket is already active")) {
        throw;
      }
      std::filesystem::remove(config_.socket_path);
    }
  }

  listener_ = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (listener_ < 0) {
    throw std::runtime_error("cannot create Unix socket");
  }
  const auto address = socketAddress(config_.socket_path);
  if (::bind(listener_, reinterpret_cast<const sockaddr *>(&address),
             sizeof(address)) != 0) {
    throw std::runtime_error("cannot bind Unix socket: " +
                             std::string(std::strerror(errno)));
  }
  owns_socket_ = true;
  if (::chmod(config_.socket_path.c_str(), 0600) != 0 ||
      ::listen(listener_, 32) != 0) {
    throw std::runtime_error("cannot configure Unix socket");
  }
  logEvent("event=reservation_service_start socket=" +
           config_.socket_path.string());

  ServiceSignalScope signals;
  stop_requested_.store(false, std::memory_order_release);
  while (!service_stop_requested &&
         !stop_requested_.load(std::memory_order_acquire)) {
    pollfd ready{.fd = listener_, .events = POLLIN, .revents = 0};
    const int poll_result = ::poll(&ready, 1, 250);
    if (poll_result < 0 && errno == EINTR) {
      continue;
    }
    if (poll_result < 0) {
      throw std::runtime_error("reservation socket poll failed");
    }
    if (poll_result == 0 || !(ready.revents & POLLIN)) {
      continue;
    }
    const int connection = ::accept4(listener_, nullptr, nullptr, SOCK_CLOEXEC);
    if (connection < 0 && errno == EINTR) {
      continue;
    }
    if (connection < 0) {
      throw std::runtime_error("reservation socket accept failed");
    }
    try {
      ucred credentials{};
      socklen_t credentials_size = sizeof(credentials);
      if (::getsockopt(connection, SOL_SOCKET, SO_PEERCRED, &credentials,
                       &credentials_size) != 0) {
        throw std::runtime_error("cannot read Unix peer credentials");
      }
      const auto request = readLine(connection);
      auto response = handleRequest(request, credentials.pid, credentials.uid);
      response.push_back('\n');
      writeAll(connection, response);
    } catch (const std::exception &error) {
      try {
        writeAll(connection, "ERROR reason=protocol_error message=" +
                                 std::string(error.what()) + "\n");
      } catch (...) {
      }
    }
    closeNoInterrupt(connection);
  }
  logEvent("event=reservation_service_stop");
}

std::string ReservationService::handleRequest(const std::string &wire,
                                              pid_t peer_pid, uid_t peer_uid) {
  if (peer_uid != ::geteuid()) {
    return "DENIED reason=peer_uid_mismatch";
  }
  const auto request = parseRequest(wire);
  if (request.operation == "PING") {
    return "OK state=ready";
  }
  if (request.operation == "STOP") {
    stop_requested_.store(true, std::memory_order_release);
    return "OK state=stopping";
  }
  const auto job_id = required(request, "job");
  if (!CgroupV2Manager::validJobId(job_id)) {
    return "DENIED reason=invalid_job_id";
  }

  if (request.operation == "REGISTER") {
    const auto base = parseUnsigned(required(request, "base"), "base");
    reservations_->registerJob(job_id, base);
    logEvent("event=job_registered job=" + job_id +
             " base_bytes=" + std::to_string(base));
    return "OK state=registered";
  }

  if (request.operation == "RECLAIM") {
    const auto path = config_.cgroup_parent / job_id;
    if (std::filesystem::exists(path) && !cgroups_.cleanupSafe(job_id)) {
      return "DENIED reason=job_still_populated";
    }
    reservations_->reclaimEmptyJob(job_id);
    logEvent("event=job_reclaimed job=" + job_id);
    return "OK state=reclaimed";
  }

  if (config_.verify_peer_cgroup && !peerBelongsToJob(peer_pid, job_id)) {
    return "DENIED reason=peer_not_in_job_cgroup";
  }

  if (request.operation == "CHECKPOINT_BEGIN") {
    const auto request_id = required(request, "request");
    const auto safety = parseUnsigned(required(request, "safety"), "safety");
    const auto state = cgroups_.snapshot(job_id);
    const auto host = monitor_.sampleHostMemory();
    auto effective_available = host.mem_available_bytes;
    for (const auto &boundary : cgroups_.ancestorMemoryBoundaries()) {
      effective_available =
          std::min(effective_available, boundary.available_bytes);
    }
    const auto result = reservations_->beginCheckpoint(
        job_id, request_id, state.memory_current_bytes, effective_available,
        safety);
    std::ostringstream response;
    response << reserveName(result.decision)
             << " effective_limit=" << result.effective_limit_bytes
             << " checkpoint_available=" << result.available_bytes
             << " reason=" << result.reason;
    if (result.reservation) {
      response << " token=" << result.reservation->token;
    }
    logEvent("event=checkpoint_begin job=" + job_id +
             " decision=" + reserveName(result.decision) +
             " reason=" + result.reason +
             " memory_current_bytes=" +
             std::to_string(state.memory_current_bytes) +
             " effective_limit_bytes=" +
             std::to_string(result.effective_limit_bytes));
    return response.str();
  }

  if (request.operation == "CHECKPOINT_END") {
    const auto token = required(request, "token");
    const auto safety = parseUnsigned(required(request, "safety"), "safety");
    const auto state = cgroups_.snapshot(job_id);
    const auto result = reservations_->endCheckpoint(
        job_id, token, state.memory_current_bytes, safety);
    logEvent("event=checkpoint_end job=" + job_id +
             " token=" + token +
             " decision=" + releaseName(result.decision) +
             " reason=" + result.reason +
             " memory_current_bytes=" +
             std::to_string(state.memory_current_bytes) +
             " effective_limit_bytes=" +
             std::to_string(result.effective_limit_bytes));
    return std::string(releaseName(result.decision)) +
           " effective_limit=" +
           std::to_string(result.effective_limit_bytes) +
           " checkpoint_available=" +
           std::to_string(result.available_bytes) +
           " reason=" + result.reason;
  }

  if (request.operation == "RESERVE") {
    const auto estimated_bytes =
        parseUnsigned(required(request, "bytes"), "bytes");
    const auto safety = parseUnsigned(required(request, "safety"), "safety");
    const auto request_id = required(request, "request");
    const auto state = cgroups_.snapshot(job_id);
    const auto inactive_file = state.memory_stat.contains("inactive_file")
                                   ? state.memory_stat.at("inactive_file")
                                   : 0;
    const auto host = monitor_.sampleHostMemory();
    auto effective_available = host.mem_available_bytes;
    std::filesystem::path limiting_ancestor;
    for (const auto &boundary : cgroups_.ancestorMemoryBoundaries()) {
      if (boundary.available_bytes < effective_available) {
        effective_available = boundary.available_bytes;
        limiting_ancestor = boundary.path;
      }
    }
    const auto result = reservations_->reserve(
        job_id, request_id, estimated_bytes, state.memory_current_bytes,
        effective_available, inactive_file, safety);
    std::ostringstream response;
    response << reserveName(result.decision)
             << " effective_limit=" << result.effective_limit_bytes
             << " borrowed_bytes=" << result.borrowed_bytes
             << " reserved_memory_available="
             << result.reserved_memory_available_bytes
             << " reason=" << result.reason;
    if (result.reservation) {
      response << " token=" << result.reservation->token;
    }
    auto log_line =
        "event=reserve job=" + job_id + " request=" + request_id +
        " estimated_bytes=" + std::to_string(estimated_bytes) +
        " safety_bytes=" + std::to_string(safety) +
        " borrowed_bytes=" + std::to_string(result.borrowed_bytes) +
        " memory_current_bytes=" + std::to_string(state.memory_current_bytes) +
        " inactive_file_bytes=" + std::to_string(inactive_file) +
        " host_mem_available_bytes=" +
        std::to_string(host.mem_available_bytes) +
        " effective_mem_available_bytes=" +
        std::to_string(effective_available) +
        " limiting_ancestor=" +
        (limiting_ancestor.empty() ? std::string("host")
                                   : limiting_ancestor.string()) +
        " effective_limit_bytes=" +
        std::to_string(result.effective_limit_bytes) +
        " reserved_memory_available_bytes=" +
        std::to_string(result.reserved_memory_available_bytes) +
        " decision=" + reserveName(result.decision) +
        " reason=" + result.reason;
    if (result.reservation) {
      log_line += " token=" + result.reservation->token;
    }
    logEvent(log_line);
    return response.str();
  }

  if (request.operation == "CANCEL") {
    const auto request_id = required(request, "request");
    const auto cancelled = reservations_->cancelPending(job_id, request_id);
    logEvent("event=reservation_cancelled job=" + job_id + " request=" +
             request_id + " cancelled=" + (cancelled ? "1" : "0"));
    return std::string("OK cancelled=") + (cancelled ? "1" : "0");
  }

  const auto state = cgroups_.snapshot(job_id);
  const auto safety = parseUnsigned(required(request, "safety"), "safety");
  if (request.operation == "SHRINK") {
    const auto token = required(request, "token");
    const auto remaining =
        parseUnsigned(required(request, "remaining"), "remaining");
    const auto inactive_file = state.memory_stat.contains("inactive_file")
                                   ? state.memory_stat.at("inactive_file")
                                   : 0;
    const auto result = reservations_->shrink(
        job_id, token, remaining, state.memory_current_bytes, inactive_file,
        safety);
    std::ostringstream response;
    response << shrinkName(result.decision)
             << " previous_borrowed_bytes=" << result.previous_borrowed_bytes
             << " borrowed_bytes=" << result.borrowed_bytes
             << " returned_bytes=" << result.returned_bytes
             << " effective_limit=" << result.effective_limit_bytes
             << " reserved_memory_available="
             << result.reserved_memory_available_bytes
             << " reason=" << result.reason;
    logEvent("event=shrink job=" + job_id + " token=" + token +
             " remaining_estimated_bytes=" + std::to_string(remaining) +
             " previous_borrowed_bytes=" +
             std::to_string(result.previous_borrowed_bytes) +
             " borrowed_bytes=" + std::to_string(result.borrowed_bytes) +
             " returned_bytes=" + std::to_string(result.returned_bytes) +
             " memory_current_bytes=" +
             std::to_string(state.memory_current_bytes) +
             " inactive_file_bytes=" + std::to_string(inactive_file) +
             " effective_limit_bytes=" +
             std::to_string(result.effective_limit_bytes) +
             " reserved_memory_available_bytes=" +
             std::to_string(result.reserved_memory_available_bytes) +
             " decision=" + shrinkName(result.decision) +
             " reason=" + result.reason);
    return response.str();
  }
  if (request.operation == "RELEASE") {
    const auto token = required(request, "token");
    const auto result = reservations_->release(
        job_id, token, state.memory_current_bytes, safety);
    std::ostringstream response;
    response << releaseName(result.decision)
             << " effective_limit=" << result.effective_limit_bytes
             << " reserved_memory_available="
             << result.reserved_memory_available_bytes
             << " reason=" << result.reason;
    logEvent("event=release job=" + job_id + " token=" + token +
             " decision=" + releaseName(result.decision) +
             " reason=" + result.reason + " memory_current_bytes=" +
             std::to_string(state.memory_current_bytes) +
             " effective_limit_bytes=" +
             std::to_string(result.effective_limit_bytes) +
             " reserved_memory_available_bytes=" +
             std::to_string(result.reserved_memory_available_bytes));
    return response.str();
  }
  if (request.operation == "RETRY") {
    const auto results = reservations_->retryPendingReleases(
        job_id, state.memory_current_bytes, safety);
    std::size_t released = 0;
    std::size_t pending = 0;
    for (const auto &result : results) {
      released += result.decision == ReleaseDecision::Released;
      pending += result.decision == ReleaseDecision::Pending;
    }
    return "OK released=" + std::to_string(released) +
           " pending=" + std::to_string(pending);
  }
  return "ERROR reason=unknown_operation";
}

bool ReservationService::peerBelongsToJob(pid_t peer_pid,
                                          const std::string &job_id) const {
  std::ifstream membership("/proc/" + std::to_string(peer_pid) + "/cgroup");
  std::string line;
  const auto expected = (cgroups_.parentPath() / job_id).lexically_normal();
  auto expected_text = expected.string();
  constexpr std::string_view cgroup_root = "/sys/fs/cgroup";
  if (std::string_view(expected_text).starts_with(cgroup_root)) {
    expected_text.erase(0, cgroup_root.size());
  }
  while (std::getline(membership, line)) {
    if (!line.starts_with("0::")) {
      continue;
    }
    const auto peer_path = line.substr(3);
    return peer_path == expected_text ||
           std::string_view(peer_path).starts_with(expected_text + "/");
  }
  return false;
}

void ReservationService::logEvent(const std::string &event) {
  if (!log_) {
    return;
  }
  const auto timestamp =
      std::chrono::duration_cast<std::chrono::milliseconds>(
          std::chrono::steady_clock::now().time_since_epoch())
          .count();
  log_ << "monotonic_time_ms=" << timestamp << ' ' << event << '\n';
  log_.flush();
}

std::string sendReservationRequest(const std::filesystem::path &socket_path,
                                   const std::string &request) {
  const int connection = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (connection < 0) {
    throw std::runtime_error("cannot create reservation client socket");
  }
  try {
    const auto address = socketAddress(socket_path);
    if (::connect(connection, reinterpret_cast<const sockaddr *>(&address),
                  sizeof(address)) != 0) {
      throw std::runtime_error("cannot connect to reservation socket " +
                               socket_path.string() + ": " +
                               std::strerror(errno));
    }
    writeAll(connection, request + "\n");
    const auto response = readLine(connection);
    closeNoInterrupt(connection);
    return response;
  } catch (...) {
    closeNoInterrupt(connection);
    throw;
  }
}
