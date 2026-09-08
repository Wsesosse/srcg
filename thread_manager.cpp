#include "thread_manager.h"

#include <algorithm>

ThreadManager::ThreadManager(std::size_t worker_count) {
    worker_count = std::max<std::size_t>(1, worker_count);
    workers_.reserve(worker_count);
    for (std::size_t index = 0; index < worker_count; ++index) {
        workers_.emplace_back(&ThreadManager::workerLoop, this);
    }
}

ThreadManager::~ThreadManager() {
    stop();
}

void ThreadManager::stop() {
    {
        std::lock_guard lock(mutex_);
        if (stopping_) {
            return;
        }
        stopping_ = true;
    }
    available_.notify_all();
    for (auto& worker : workers_) {
        if (worker.joinable()) {
            worker.join();
        }
    }
}

void ThreadManager::workerLoop() {
    while (true) {
        std::function<void()> task;
        {
            std::unique_lock lock(mutex_);
            available_.wait(lock, [this] { return stopping_ || !tasks_.empty(); });
            if (stopping_ && tasks_.empty()) {
                return;
            }
            task = std::move(tasks_.front());
            tasks_.pop();
        }
        task();
    }
}
