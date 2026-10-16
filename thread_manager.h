#ifndef SRCG_THREAD_MANAGER_H
#define SRCG_THREAD_MANAGER_H

#include <condition_variable>
#include <cstddef>
#include <functional>
#include <future>
#include <mutex>
#include <queue>
#include <stdexcept>
#include <thread>
#include <type_traits>
#include <utility>
#include <vector>

class ThreadManager {
public:
    explicit ThreadManager(std::size_t worker_count = std::thread::hardware_concurrency());
    ~ThreadManager();

    ThreadManager(const ThreadManager&) = delete;
    ThreadManager& operator=(const ThreadManager&) = delete;

    template <typename Function, typename... Args>
    auto submit(Function&& function, Args&&... args)
        -> std::future<std::invoke_result_t<Function, Args...>> {
        using Result = std::invoke_result_t<Function, Args...>;
        auto task = std::make_shared<std::packaged_task<Result()>>(
            std::bind(std::forward<Function>(function), std::forward<Args>(args)...));
        auto result = task->get_future();
        {
            std::lock_guard lock(mutex_);
            if (stopping_) {
                throw std::runtime_error("cannot submit work to a stopped ThreadManager");
            }
            tasks_.emplace([task] { (*task)(); });
        }
        available_.notify_one();
        return result;
    }

    void stop();
    [[nodiscard]] std::size_t workerCount() const noexcept { return workers_.size(); }

private:
    void workerLoop();

    mutable std::mutex mutex_;
    std::condition_variable available_;
    std::queue<std::function<void()>> tasks_;
    std::vector<std::thread> workers_;
    bool stopping_{false};
};

#endif
