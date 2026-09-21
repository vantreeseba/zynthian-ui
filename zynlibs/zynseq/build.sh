#!/bin/bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

BUILD_TYPE="Release"
if [ $# -ge 1 ]; then
    BUILD_TYPE=$1
fi

# Ableton Link is vendored as a git submodule. Pulling the repo doesn't move it,
# so bring it to the pinned commit whenever it is missing or has fallen behind
LINK_PINNED=$(git -C "$DIR/../.." ls-tree HEAD zynlibs/link 2>/dev/null | awk '{print $3}')
LINK_CURRENT=""
if [ -f "$DIR/../link/AbletonLinkConfig.cmake" ]; then
	LINK_CURRENT=$(git -C "$DIR/../link" rev-parse HEAD 2>/dev/null)
fi
if [ -z "$LINK_CURRENT" ] || { [ -n "$LINK_PINNED" ] && [ "$LINK_CURRENT" != "$LINK_PINNED" ]; }; then
	echo "Updating Ableton Link submodule..."
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
