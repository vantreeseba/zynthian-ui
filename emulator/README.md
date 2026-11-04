# Desktop Emulator

Run zynthian-ui on a Linux desktop using the `EMULATOR` wiring layout — no
Raspberry Pi, no GPIO, no real Zynthian hardware. Useful for UI/styling work
and quick navigation testing.

## Requirements

- Linux with an X11 session (the UI is a Tk app; tested at 800x480).
- PipeWire with its JACK replacement (`pipewire-jack`), or a running jackd.
- Python 3 with tkinter, `python3-venv`.
- CMake + a C/C++ toolchain.
- Dev libraries for the native builds: JACK (or pipewire-jack dev files),
  liblo, sndfile, samplerate, rubberband, mpg123, id3tag.

On Debian/Ubuntu, roughly:

```sh
sudo apt install build-essential cmake python3-venv python3-tk \
    libjack-jackd2-dev liblo-dev libsndfile1-dev libsamplerate0-dev \
    librubberband-dev libmpg123-dev libid3tag0-dev
```

## Setup (once)

```sh
./setup.sh          # python venv, pip deps, stub modules, fonts
./build-native.sh   # zyncoder + zynlibs shared libraries
```

## Run

```sh
./start.sh
```

The UI opens in an 800x480 Tk window (its WM window name is literally `tk`).
On first run, `emulator/data/` is seeded from `emulator/template/` — that
directory is gitignored and acts as the fake `/zynthian` tree (config,
my-data, snapshots...). Delete `emulator/data/` to reset to a clean state.

Set `DISPLAY_WIDTH` / `DISPLAY_HEIGHT` before `./start.sh` for other sizes.

## Keyboard bindings

Defined in `template/config/keybinding.json` (copied into `data/` on first
run):

| Key | Action |
|-----|--------|
| F1 | Chain manager |
| F2 | Launcher (clip launcher) |
| F3 | Pattern editor |
| F4 | Mixer |
| F5 | Zynpad |
| F6 | Snapshots |
| F7 | Admin |
| F8 | Audio mixer |
| Enter | Select (switch 3 short) |
| Escape / Backspace | Back (switch 1 short) |
| i / k / o / l | Switches 0-3 |
| Arrow keys | Navigation |

## Caveats

- **pyliblo3 / lilv stubs**: if these can't be installed (no wheel for the
  current Python; lilv isn't on PyPI), `setup.sh` drops no-op stubs into the
  venv. OSC send/receive does nothing and the LV2 plugin list is empty. If
  the real packages are importable, the stubs are skipped.
- **Engines**: most synth engines aren't installed on a desktop, so adding
  chains with real engines will fail; the UI, mixer, sequencer, and pattern
  editor all work.
- **PipeWire quirk**: PipeWire's JACK MIDI ports report no ALSA aliases;
  the repo carries guards in `zynautoconnect` for this.
- `build-native.sh` temporarily rewrites `add_definitions(-Werror)` in the
  zynlibs CMakeLists during the build (newer GCCs fail the Pi's -Werror) and
  restores them via `git checkout` afterwards.

## Layout

```
emulator/
├── env.sh           # environment (sourced by start.sh)
├── setup.sh         # one-time python/font setup
├── build-native.sh  # builds zyncoder + zynlibs .so files
├── start.sh         # launches the UI
├── stubs/           # pyliblo3/lilv fallback modules
├── template/        # pristine config tree, copied to data/ on first run
├── data/            # runtime fake /zynthian tree (gitignored)
└── venv/            # python virtualenv (gitignored)
```
