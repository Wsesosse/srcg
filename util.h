#ifndef UTIL_H
#define UTIL_H

#include <chrono>
#include <iostream>

namespace chrono_utils{
    template<typename Func>
    void runwithtimer(const Func &func){
        auto start = std::chrono::high_resolution_clock::now();
        func();
        auto end = std::chrono::high_resolution_clock::now();
        std::chrono::duration<double> elapsed = end - start;
        std::cout << "Execution time: " << elapsed.count() << " seconds" << std::endl;
    }
}

#endif // UTIL_H