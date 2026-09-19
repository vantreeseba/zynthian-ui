#!/bin/bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

BUILD_TYPE="Release"
if [ $# -ge 1 ]; then
    BUILD_TYPE=$1
fi

# Ableton Link is vendored as a git submodule
if [ ! -f "$DIR/../link/AbletonLinkConfig.cmake" ]; then
	echo "Fetching Ableton Link submodule..."
	git -C "$DIR/../.." submodule update --init --recursive zynlibs/link || exit 1
fi

pushd $DIR
	if [ ! -d build ]; then
		mkdir build
	fi
	pushd build
		cmake -DCMAKE_BUILD_TYPE="$BUILD_TYPE" -D CMAKE_CXX_FLAGS="-Wno-psabi" ..
		make
		success=$?
	popd
popd
exit $success
