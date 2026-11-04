#!/bin/bash
# Start zynthian-ui on the desktop with the EMULATOR wiring layout.
# Prerequisites (once): ./setup.sh and ./build-native.sh
set -e

EMU_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$EMU_DIR")"

source "$EMU_DIR/env.sh"

# --- sanity checks -------------------------------------------------------------
if [ ! -x "$EMU_DIR/venv/bin/python" ]; then
    echo "ERROR: no venv at emulator/venv — run ./setup.sh first" >&2
    exit 1
fi
if [ ! -f "$REPO_DIR/zyncoder/build/libzyncore.so" ]; then
    echo "ERROR: zyncoder/build/libzyncore.so missing — run ./build-native.sh first" >&2
    exit 1
fi
for lib in zynseq zynsmf zynmixer zynclippy zynaudioplayer; do
    if ! ls "$REPO_DIR/zynlibs/$lib/build/"lib*.so >/dev/null 2>&1; then
        echo "ERROR: zynlibs/$lib/build/lib*.so missing — run ./build-native.sh first" >&2
        exit 1
    fi
done

# --- seed the runtime data tree on first run -----------------------------------
EMU_DATA="$EMU_DIR/data"
if [ ! -d "$EMU_DATA/config" ]; then
    echo "==> Seeding emulator data tree from template"
    mkdir -p "$EMU_DATA"
    cp -r "$EMU_DIR/template/." "$EMU_DATA/"
fi
# Runtime dirs the UI expects to exist (cheap to ensure on every start).
mkdir -p \
    "$EMU_DATA/config/jalv" \
    "$EMU_DATA/config/wiring-profiles" \
    "$EMU_DATA/zynthian-my-data/capture" \
    "$EMU_DATA/zynthian-my-data/files" \
    "$EMU_DATA/zynthian-my-data/midi-profiles" \
    "$EMU_DATA/zynthian-my-data/preset-favorites" \
    "$EMU_DATA/zynthian-my-data/snapshots/000" \
    "$EMU_DATA/zynthian-data" \
    "$EMU_DATA/zynthian-sw" \
    "$EMU_DATA/zynthian-sys/config"

# PipeWire must be up to provide JACK.
if ! "$EMU_DIR/venv/bin/python" -c "import jack; jack.Client('emu-probe', no_start_server=True).close()" 2>/dev/null; then
    echo "WARNING: could not connect to a JACK server (is PipeWire running?)" >&2
fi

cd "$REPO_DIR"
exec "$EMU_DIR/venv/bin/python" zynthian_main.py "$@"
