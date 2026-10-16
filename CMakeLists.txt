cmake_minimum_required(VERSION 3.17)

project(srcg VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Threads REQUIRED)

add_library(srcg_core
    cgroup_manager.cpp
    linux_monitor.cpp
    memory_size.cpp
    process_supervisor.cpp
    reservation_ipc.cpp
    reservation_manager.cpp
    srcg_daemon.cpp
    thread_manager.cpp)
target_include_directories(srcg_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(srcg_core PUBLIC Threads::Threads)
target_compile_options(srcg_core PRIVATE -Wall -Wextra -Wpedantic)

add_executable(srcg srcg_cli.cpp)
target_link_libraries(srcg PRIVATE srcg_core)
target_compile_options(srcg PRIVATE -Wall -Wextra -Wpedantic)

add_executable(srcg_memory_probe benchmarks/memory_probe.cpp)
target_link_libraries(srcg_memory_probe PRIVATE srcg_core)
target_compile_options(srcg_memory_probe PRIVATE -Wall -Wextra -Wpedantic)

# NVML is deliberately optional in Milestone 1. RAM/cgroup management must
# remain usable on CPU-only development and test machines.
find_package(CUDAToolkit QUIET)
if (CUDAToolkit_FOUND)
    add_library(srcg_gpu_monitor gpu_monitor.cpp)
    target_include_directories(srcg_gpu_monitor PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
    target_link_libraries(srcg_gpu_monitor PUBLIC CUDA::nvml)
    target_compile_options(srcg_gpu_monitor PRIVATE -Wall -Wextra -Wpedantic)
else()
    message(STATUS "CUDA Toolkit not found: skipping optional NVML component")
endif()

include(CTest)
if (BUILD_TESTING)
    add_executable(srcg_framework_tests tests/framework_tests.cpp)
    target_link_libraries(srcg_framework_tests PRIVATE srcg_core)
    add_test(NAME srcg_framework_tests COMMAND srcg_framework_tests)

    add_executable(srcg_cgroup_tests tests/cgroup_manager_tests.cpp)
    target_link_libraries(srcg_cgroup_tests PRIVATE srcg_core)
    add_test(NAME srcg_cgroup_tests COMMAND srcg_cgroup_tests)

    add_executable(srcg_process_tests tests/process_supervisor_tests.cpp)
    target_link_libraries(srcg_process_tests PRIVATE srcg_core)
    add_test(NAME srcg_process_tests COMMAND srcg_process_tests)

    add_executable(srcg_reservation_tests tests/reservation_manager_tests.cpp)
    target_link_libraries(srcg_reservation_tests PRIVATE srcg_core)
    add_test(NAME srcg_reservation_tests COMMAND srcg_reservation_tests)

    add_executable(srcg_reservation_ipc_tests tests/reservation_ipc_tests.cpp)
    target_link_libraries(srcg_reservation_ipc_tests PRIVATE srcg_core)
    add_test(NAME srcg_reservation_ipc_tests COMMAND srcg_reservation_ipc_tests)
    set_tests_properties(srcg_reservation_ipc_tests PROPERTIES SKIP_RETURN_CODE 77)

    add_executable(srcg_cgroup_integration_tests tests/cgroup_integration_tests.cpp)
    target_link_libraries(srcg_cgroup_integration_tests PRIVATE srcg_core)
    add_test(NAME srcg_cgroup_integration_tests COMMAND srcg_cgroup_integration_tests)
    set_tests_properties(srcg_cgroup_integration_tests PROPERTIES SKIP_RETURN_CODE 77)
endif()
