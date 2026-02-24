cmake_minimum_required(VERSION 3.10)

project(srcg)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(srcg
    src/cgroup.cpp
)

target_compile_options(srcg PRIVATE -Wall -Wextra -Wpedantic)
