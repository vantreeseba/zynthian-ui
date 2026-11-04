# Environment for running zynthian-ui on a desktop with the EMULATOR wiring layout.
# Source this (directly or via start.sh); do not execute.

EMU_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$EMU_DIR")"
EMU_DATA="$EMU_DIR/data"

export ZYNTHIAN_WIRING_LAYOUT="EMULATOR"
export ZYNTHIAN_DIR="$EMU_DATA"
export ZYNTHIAN_CONFIG_DIR="$EMU_DATA/config"
export ZYNTHIAN_MY_DATA_DIR="$EMU_DATA/zynthian-my-data"
export ZYNTHIAN_DATA_DIR="$EMU_DATA/zynthian-data"
export ZYNTHIAN_SYS_DIR="$EMU_DATA/zynthian-sys"
export ZYNTHIAN_SW_DIR="$EMU_DATA/zynthian-sw"
export ZYNTHIAN_UI_DIR="$REPO_DIR"

export DISPLAY_WIDTH="${DISPLAY_WIDTH:-800}"
export DISPLAY_HEIGHT="${DISPLAY_HEIGHT:-480}"
export ZYNTHIAN_UI_RESTORE_LAST_STATE="${ZYNTHIAN_UI_RESTORE_LAST_STATE:-0}"
export ZYNTHIAN_UI_ENABLE_CURSOR=1
# zynthian_engine_jalv does a substring check on this; it must be set.
export RBPI_VERSION="Desktop Emulator"

# Use PipeWire's JACK replacement instead of jackd.
_pwjack="/usr/lib/$(gcc -dumpmachine 2>/dev/null || echo x86_64-linux-gnu)/pipewire-0.3/jack"
if [ -d "$_pwjack" ]; then
    export LD_LIBRARY_PATH="$_pwjack${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
unset _pwjack

# PipeWire's socket lives in the real per-user runtime dir; some shells
# inherit a bogus XDG_RUNTIME_DIR (e.g. /run/user without the uid).
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# CLAP plugins: user-installed ones, plus the test plugin built with the host.
export CLAP_PATH="$EMU_DATA/zynthian-plugins/clap:$REPO_DIR/zynlibs/zynclap/build/test"

export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
export DISPLAY="${DISPLAY:-:0.0}"
