# c_core/CMakeLists.txt — сборка нативного ядра libcompass_core.so через CMake.
# Дублирует флаги scripts/build_c_core.py (канонический путь для рантайм-пересборки
# без установленного cmake); расхождение флагов между двумя путями недопустимо.
#
# Использование:
#   cmake -S c_core -B c_core/build && cmake --build c_core/build
#   (артефакт: c_core/build/libcompass_core.so — при необходимости скопировать в c_core/)
cmake_minimum_required(VERSION 3.16)
project(compass_core C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# Предупреждение компилятора = блокер сборки (медицинское ядро; так же ведёт себя
# scripts/build_c_core.py). Новые модули чиним, а не глушим.
add_compile_options(-O3 -Wall -Wextra -Werror)

# Архитектурные флаги — как в build_c_core.py: AVX2 на x86_64, -mcpu=native на ARM.
if(CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86_64|amd64|AMD64)$")
    include(CheckCCompilerFlag)
    check_c_compiler_flag("-mavx2" COMPASS_HAS_AVX2)
    if(COMPASS_HAS_AVX2)
        add_compile_options(-mavx2)
    endif()
elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "^(aarch64|arm|ARM)$")
    include(CheckCCompilerFlag)
    check_c_compiler_flag("-mcpu=native" COMPASS_HAS_MCPU_NATIVE)
    if(COMPASS_HAS_MCPU_NATIVE)
        add_compile_options(-mcpu=native)
    endif()
endif()

file(GLOB COMPASS_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/*.c")

add_library(compass_core SHARED ${COMPASS_SOURCES})
set_target_properties(compass_core PROPERTIES OUTPUT_NAME "compass_core")
target_link_libraries(compass_core PRIVATE m)
