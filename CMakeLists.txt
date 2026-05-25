cmake_minimum_required(VERSION 3.16)
project(omu_kosen_route_core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

find_package(pybind11 REQUIRED)

pybind11_add_module(route_core src/route_core.cpp)

set(PYBIND11_VENV_INCLUDE
    "${CMAKE_CURRENT_SOURCE_DIR}/.venv/lib/python3.12/site-packages/pybind11/include")
if(EXISTS "${PYBIND11_VENV_INCLUDE}/pybind11/pybind11.h")
    target_include_directories(route_core PRIVATE "${PYBIND11_VENV_INCLUDE}")
endif()

# Pythonから import route_core できるように、ビルド成果物を src/ に置きます。
set_target_properties(route_core PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/src"
)
