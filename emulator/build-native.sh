#!/bin/bash
# Build the native libraries needed by the desktop emulator:
#   zyncoder/build/libzyncore.so and zynlibs/*/build/lib*.so
#
# Notes for desktop toolchains (tested with GCC 15 on Ubuntu):
#  - zynlibs pass -Werror; newer GCCs emit warnings the Pi toolchain doesn't,
#    so -Werror is temporarily neutralised in each CMakeLists and restored after.
#  - zynseq needs the single-header nlohmann/json.hpp (fetched if not present).
#  - Requires dev packages for: jack (or pipewire-jack), liblo, sndfile,
#    samplerate, rubberband, mpg123, id3tag.
set -e

EMU_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$EMU_DIR")"
CACHE="$EMU_DIR/.cache"
JOBS="$(nproc 2>/dev/null || echo 4)"

# --- nlohmann/json.hpp (header-only, needed by zynseq) ------------------------
NLOHMANN_INC=""
if [ ! -f /usr/include/nlohmann/json.hpp ]; then
    if [ ! -f "$CACHE/include/nlohmann/json.hpp" ]; then
        echo "==> Fetching nlohmann/json.hpp"
        mkdir -p "$CACHE/include/nlohmann"
        curl -fsSL -o "$CACHE/include/nlohmann/json.hpp" \
            "https://github.com/nlohmann/json/releases/download/v3.11.3/json.hpp"
    fi
    NLOHMANN_INC="$CACHE/include"
fi

# --- zyncoder (DUMMIES wiring: no GPIO, jack+liblo only) -----------------------
echo "==> Building zyncoder"
(
    cd "$REPO_DIR/zyncoder"
    ZYNTHIAN_WIRING_LAYOUT=DUMMIES cmake -B build -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build build -j "$JOBS"
)

# --- zynlibs -------------------------------------------------------------------
build_zynlib() {
    local name="$1"; shift
    local dir="$REPO_DIR/zynlibs/$name"
    echo "==> Building $name"
    # Neutralise -Werror for the duration of the build, then restore.
    sed -i 's/add_definitions(-Werror)/add_definitions(-Wno-error)/' "$dir/CMakeLists.txt"
    trap 'git -C "'"$REPO_DIR"'" checkout -- "zynlibs/'"$name"'/CMakeLists.txt"' RETURN
    (
        cd "$dir"
        cmake -B build -DCMAKE_BUILD_TYPE=Release --compile-no-warning-as-error "$@" >/dev/null
        cmake --build build -j "$JOBS"
    )
}

build_zynlib zynsmf
build_zynlib zynmixer
build_zynlib zynclippy
# CLAP host: only present once the CLAP work is merged into this branch.
if [ -d "$REPO_DIR/zynlibs/zynclap" ]; then
    build_zynlib zynclap
fi
if [ -n "$NLOHMANN_INC" ]; then
    build_zynlib zynseq -DCMAKE_CXX_FLAGS="-I$NLOHMANN_INC"
else
    build_zynlib zynseq
fi
build_zynlib zynaudioplayer \
    -DCMAKE_C_FLAGS="-Wno-error=format -Wno-error=format-security -Wno-error=unused-result" \
    -DCMAKE_CXX_FLAGS="-Wno-error=format -Wno-error=format-security -Wno-error=unused-result"

echo "==> All native libraries built."
