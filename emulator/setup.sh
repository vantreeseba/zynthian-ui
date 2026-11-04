#!/bin/bash
# One-time setup for the desktop emulator: python venv, UI fonts, stub modules.
# Native libraries are built separately by build-native.sh.
set -e

EMU_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$EMU_DIR")"
VENV="$EMU_DIR/venv"

echo "==> Creating venv at $VENV"
python3 -m venv --system-site-packages "$VENV"
PIP="$VENV/bin/pip"
PY="$VENV/bin/python"

# Core dependencies. Installed one at a time, best effort: some (device-only
# or without wheels for the current python) are fine to skip on a desktop.
DEPS=(
    JACK-Client alsa-midi oyaml Levenshtein ffmpeg-python tkinterweb
    pexpect mutagen pyusb pyalsaaudio evdev rdflib python-xlib
    websocket-client requests psutil numpy
)
for dep in "${DEPS[@]}"; do
    echo "==> pip install $dep"
    "$PIP" install --quiet "$dep" || echo "WARNING: could not install $dep (continuing)"
done

# The system pillow usually lacks ImageTk; force a full copy into the venv.
echo "==> pip install pillow (with ImageTk)"
"$PIP" install --quiet --ignore-installed pillow

# Stub modules for packages that can't be installed on a typical desktop:
#  - pyliblo3: no wheel for recent pythons (OSC send/recv becomes a no-op)
#  - lilv: not pip-installable (LV2 plugin list will be empty)
# Only installed if the real module is missing, so a real install wins.
SITE_PACKAGES="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
for stub in pyliblo3 lilv; do
    if "$PY" -c "import $stub" 2>/dev/null; then
        echo "==> $stub available, skipping stub"
    else
        echo "==> Installing $stub stub"
        cp "$EMU_DIR/stubs/$stub.py" "$SITE_PACKAGES/"
    fi
done

# Make the repo's bundled fonts (Exo 2, ForkAwesome, ...) visible to Tk.
echo "==> Linking repo fonts into ~/.local/share/fonts"
mkdir -p "$HOME/.local/share/fonts"
ln -sfn "$REPO_DIR/fonts" "$HOME/.local/share/fonts/zynthian-ui-fonts"
fc-cache -f >/dev/null

echo "==> Done. Build native libs with ./build-native.sh, then run ./start.sh"
