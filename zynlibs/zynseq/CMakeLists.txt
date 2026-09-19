cmake_minimum_required(VERSION 3.20)
project(zynseq)

include(CheckIncludeFiles)
include(CheckLibraryExists)
find_package(mimalloc REQUIRED)

# Ableton Link (header only, vendored as a git submodule)
include(${CMAKE_CURRENT_SOURCE_DIR}/../link/AbletonLinkConfig.cmake)

link_directories(/usr/local/lib)
set (CMAKE_BUILD_TYPE Debug)

add_library(zynseq SHARED zynseq.h zynseq.cpp sequencemanager.cpp pattern.cpp sequence.cpp timebase.cpp track.cpp linksync.cpp)
add_definitions(-Werror)
target_link_libraries(zynseq jack mimalloc Ableton::Link)

install(TARGETS zynseq LIBRARY DESTINATION lib)
