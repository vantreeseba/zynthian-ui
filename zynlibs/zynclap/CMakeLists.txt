cmake_minimum_required(VERSION 3.20)
project(zynclap C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

link_directories(/usr/local/lib)

# CLAP is header-only: use the system headers if installed, otherwise fetch them
find_path(CLAP_INCLUDE_DIR clap/clap.h)
if(NOT CLAP_INCLUDE_DIR)
    include(FetchContent)
    # SOURCE_SUBDIR points nowhere so only the headers are fetched, nothing is built or installed
    FetchContent_Declare(clap
        GIT_REPOSITORY https://github.com/free-audio/clap.git
        GIT_TAG 1.2.2
        GIT_SHALLOW TRUE
        SOURCE_SUBDIR headers-only)
    FetchContent_MakeAvailable(clap)
    set(CLAP_INCLUDE_DIR ${clap_SOURCE_DIR}/include)
endif()
message(STATUS "Using CLAP headers from ${CLAP_INCLUDE_DIR}")

add_executable(zynclap zynclap.cpp)
target_include_directories(zynclap PRIVATE ${CLAP_INCLUDE_DIR})
target_compile_options(zynclap PRIVATE -Wall)
target_link_libraries(zynclap jack ${CMAKE_DL_LIBS} pthread)

# Test plugin
add_library(zyntest MODULE test/zyntest.c)
target_include_directories(zyntest PRIVATE ${CLAP_INCLUDE_DIR})
set_target_properties(zyntest PROPERTIES
    PREFIX ""
    SUFFIX ".clap"
    C_VISIBILITY_PRESET hidden
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/test)

install(TARGETS zynclap RUNTIME DESTINATION bin)
