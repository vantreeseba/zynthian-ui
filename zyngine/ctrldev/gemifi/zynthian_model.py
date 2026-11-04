# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Control Device Driver
#
# gemifi driver: Zynthian's state behind the gemifi role mapping
#
# Copyright (C) 2026 Benjamin Van Treese <vantreeseba@gmail.com>
#
# ******************************************************************************
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of
# the License, or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# For a full copy of the GNU General Public License see the LICENSE.txt file.
#
# ******************************************************************************

"""Adapts Zynthian to the abstract song in mapping.py (gemifi hosts/zynthian/README.md §5, §6).

No Zynthian imports: everything comes in through the state manager (and the callbacks) the
driver passes, so the tests can use fakes. Chains are the tracks, the chain mixer is the mixer,
zynseq phrases are the scenes, Zynthian's four zynpots are device_param_1..4, and the clip grid
is zynpad's launcher grid: columns are chains (in mixer order), rows are phrases.
"""

import re
import unicodedata

from . import protocol as P
from .mapping import Mapping, Param, Song, Track, V14, OFF, ON, _ButtonRole, _ButtonBinding, _LedBinding

PHRASE_CHANNEL = 32         # zynseq's phrase launcher "channel" (zynseq.PHRASE_CHANNEL)
NUM_ZYNPOTS = 4
METRO_AUTO = 1              # zynseq metronome mode: click while the transport rolls
TEMPO_MIN, TEMPO_MAX = 10.0, 500.0

# zynseq play states (zynseq.SEQ_*): stopped, or playing / recording, or waiting for the sync
# point to start or stop (queued: STARTING, STOPPING, FORCED_STOP, STOPPING_SYNC, CHILD_STOPPING,
# STARTING_RECORD, STOPPING_RECORD).
SEQ_STOPPED = 0
SEQ_QUEUED = frozenset((2, 3, 4, 5, 8, 10, 11))
SEQ_GROUP_CLIPPY = 16       # sequences in groups >= 16 are clippy (audio clip) pads

# Clip grid LEDs (FB_LED_RGB r, g, b, blink): no sequence, a stopped one, a playing one, and
# one whose start or stop is queued (blue, blinking, as daw-scripts.md 5.9 suggests).
CLIP_NONE, CLIP_STOPPED, CLIP_PLAYING, CLIP_QUEUED = 0, 1, 2, 3
CLIP_RGB = {CLIP_NONE: (0, 0, 0, False), CLIP_STOPPED: (0, 127, 127, False),
            CLIP_PLAYING: (0, 0, 127, False), CLIP_QUEUED: (0, 0, 127, True)}

# Zynthian's switches (zynthian_gui.zynswitch_short / _bold / _long): 0 the layer / home switch,
# 1 back, 2 snapshot / alt, 3 select. The press type comes from the hold time measured by the
# device (EVENT t_ms), against Zynthian's own thresholds (zynthian_gui_config).
NAV_SWITCHES = {"nav_home": 0, "nav_back": 1, "nav_alt": 2, "nav_select": 3}
SWITCH_BOLD_US, SWITCH_LONG_US = 300 * 1000, 2000 * 1000    # Zynthian's defaults
NAV_ARROWS = {"nav_up": "ARROW_UP", "nav_down": "ARROW_DOWN", "nav_left": "ARROW_LEFT",
              "nav_right": "ARROW_RIGHT"}
ZYNPOT_SELECT = 3           # the select knob: nav_scroll
HOST_NAMESPACE = "zynthian."
_CUIA_ROLE = re.compile(r"^zynthian\.([a-z0-9_]+)$")
_CLIP_ROLE = re.compile(r"^clip_([1-8])_([1-8])$")
GRID_STEPS = {"grid_left": (-1, 0), "grid_right": (1, 0), "grid_up": (0, -1), "grid_down": (0, 1)}
TEXT_HOLD_TICKS = 15        # a knob's value stays on the display ~1.5 s, then the chain name
DISPLAY_NAME_ROLES = ("", "track_name")     # show the selected chain's name
DISPLAY_PARAM_ROLE = "param"                # ... and the value of the control being moved


def param_label(role):
    """A short name for the param display: V3 (volume_3), P3 (pan_3), M (master), V / P
    (selected track); "" when the value says it already (tempo)."""
    if role == "master_volume":
        return "M"
    if role in ("track_volume", "track_pan"):
        return role[6].upper()
    base, _sep, num = role.rpartition("_")
    if base in ("volume", "pan") and num.isdigit():
        return base[0].upper() + num
    return ""


def clip_role(role):
    """'clip_3_2' -> (3, 2): track (column) and scene (row), 1-based; else (None, None)."""
    m = _CLIP_ROLE.match(role or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def press_type(held_ms, bold_us=SWITCH_BOLD_US, long_us=SWITCH_LONG_US):
    """ZYNSWITCH press type for a press held `held_ms`: 'S' short, 'B' bold, 'L' long, with the
    comparisons of zynthian_gui.zynswitch_timing()."""
    us = held_ms * 1000
    if us > long_us:
        return "L"
    if us > bold_us:
        return "B"
    return "S"

# Roles with no Zynthian equivalent in this fork (README §5 "to check", nothing found):
# they're logged once and ignored, as daw-scripts.md §6 asks.
UNSUPPORTED_BUTTON_ROLES = frozenset((
    "loop", "overdub", "automation_arm", "undo", "redo", "rewind", "fast_forward",
    "device_prev", "device_next", "device_bank_prev", "device_bank_next", "device_on",
    "stop_all_clips"))
UNSUPPORTED_CONT_ROLES = frozenset(("cue_volume", "crossfader", "send_a", "send_b"))


def _norm(zctrl):
    lo = zctrl.value_min or 0
    hi = zctrl.value_max if zctrl.value_max is not None else 1
    if hi == lo:
        return 0.0
    return min(1.0, max(0.0, (float(zctrl.value) - lo) / (hi - lo)))


def _display_text(text, cols):
    """`text` as printable ASCII (accents dropped), cut and padded to `cols` characters."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    text = "".join(ch if " " <= ch <= "~" else " " for ch in text).strip()
    return text[:cols].ljust(cols)


def _denorm(zctrl, norm):
    lo = zctrl.value_min or 0
    hi = zctrl.value_max if zctrl.value_max is not None else 1
    return lo + norm * (hi - lo)


class ZctrlParam(Param):
    """A Zynthian controller (a mixer strip's level or balance). Looked up on every use, so a
    chain that goes away just reads as 0."""

    def __init__(self, lookup, key, bipolar=False):
        self._lookup = lookup
        self._key = key
        self.bipolar = bipolar

    def display_text(self):
        """Short text for the param display: level as a percentage, balance as L20 / C / R20."""
        z = self._lookup()
        if z is None:
            return ""
        if self.bipolar:
            v = round(float(z.value) * 100 / max(abs(z.value_min or 0), abs(z.value_max or 1), 1e-9))
            return "C" if v == 0 else ("L%d" % -v if v < 0 else "R%d" % v)
        return "%d%%" % round(_norm(z) * 100)

    @property
    def key(self):
        return self._key

    def get_norm(self):
        z = self._lookup()
        return _norm(z) if z is not None else 0.0

    def set_norm(self, value):
        z = self._lookup()
        if z is not None and not z.readonly:
            z.set_value(_denorm(z, value))

    def nudge(self, delta):
        """Encoders step by the controller's own nudge factor, as Zynthian's knobs do."""
        z = self._lookup()
        if z is not None and not z.readonly:
            z.nudge(delta)


class TempoParam(Param):
    """zynseq's tempo, 1 BPM per encoder detent."""

    def __init__(self, zynseq):
        self.zynseq = zynseq

    @property
    def key(self):
        return "tempo"

    def display_text(self):
        return "%dbpm" % round(float(self.zynseq.get_tempo()))

    def get_norm(self):
        return (float(self.zynseq.get_tempo()) - TEMPO_MIN) / (TEMPO_MAX - TEMPO_MIN)

    def set_norm(self, value):
        self.zynseq.set_tempo(round(TEMPO_MIN + value * (TEMPO_MAX - TEMPO_MIN)))

    def nudge(self, delta):
        self.zynseq.set_tempo(min(TEMPO_MAX, max(TEMPO_MIN, self.zynseq.get_tempo() + delta)))


class ZynpotTarget(Param):
    """One of Zynthian's four rotary controls on whatever screen is shown. Zynthian owns the
    value, so there's nothing to show back: no FB_VALUE, ring off."""

    def __init__(self, send_cuia, index):
        self._send_cuia = send_cuia
        self.index = index

    @property
    def key(self):
        return ("zynpot", self.index)

    def get_norm(self):
        return 0.0

    def set_norm(self, value):
        self._send_cuia("ZYNPOT_ABS", [self.index, min(1.0, max(0.0, value))])

    def nudge(self, delta):
        self._send_cuia("ZYNPOT", [self.index, int(delta)])


class ZynTrack(Track):
    FLAGS = {"mute": "mute", "solo": "solo", "arm": "record"}

    def __init__(self, chain):
        self.chain = chain

    @property
    def key(self):
        return self.chain.chain_id

    @property
    def name(self):
        """The chain's short name, as on the mixer strip."""
        return self.chain.get_name() or ""

    def _zctrl(self, symbol):
        proc = getattr(self.chain, "zynmixer_proc", None)
        if proc is None:
            return None
        return proc.controllers_dict.get(symbol)

    def get_flag(self, name):
        z = self._zctrl(self.FLAGS.get(name, name))
        return bool(z is not None and z.value)

    def set_flag(self, name, on):
        z = self._zctrl(self.FLAGS.get(name, name))
        if z is not None and not z.readonly:
            z.set_value(1 if on else 0)

    def can_arm(self):
        z = self._zctrl("record")
        return z is not None and not z.readonly

    def param(self, name):
        cid = self.chain.chain_id
        if name == "volume":
            return ZctrlParam(lambda: self._zctrl("level"), (cid, "level"))
        if name == "pan":
            return ZctrlParam(lambda: self._zctrl("balance"), (cid, "balance"), bipolar=True)
        return None


class ZynSong(Song):
    """`chain_ids` returns the driver's filtered chain ids in display order (the mixer bank).
    `midi_transport` True drives the MIDI (SMF) player/recorder instead of the audio ones."""

    def __init__(self, state_manager, chain_ids, midi_transport=False, pad_press=None, pad_release=None,
                 switch_thresholds=None):
        """`pad_press(phrase, midi_chan)` / `pad_release(...)`: how a clip pad launches, normally the
        zynpad base class's on_pad_press / on_pad_release (record modes, hold-to-clear). Without
        them a press just toggles the sequence. `switch_thresholds()` -> (bold_us, long_us)."""
        self.sm = state_manager
        self.chain_ids = chain_ids
        self.midi_transport = midi_transport
        self._metro_mode = METRO_AUTO   # the mode metronome "on" goes back to
        self._pad_press = pad_press
        self._pad_release = pad_release
        self._switch_thresholds = switch_thresholds

    @property
    def chains(self):
        return self.sm.chain_manager.chains

    @property
    def zynseq(self):
        return self.sm.zynseq

    def cuia(self, name, params=None):
        self.sm.send_cuia(name, params=params)

    # ---- transport ----

    def get_flag(self, name):
        sm = self.sm
        if name == "playing":
            return bool(sm.status_midi_player if self.midi_transport else sm.status_audio_player)
        if name == "record":
            if self.midi_transport:
                return bool(sm.status_midi_recorder)
            return bool(getattr(sm.audio_recorder, "rec_proc", None))
        if name == "metronome":
            return bool(self.zynseq.zctrl_metro_mode.value)
        if name == "session_record":
            return bool(sm.session_record_mode)
        return False

    def set_flag(self, name, on):
        if self.get_flag(name) == bool(on):
            return
        kind = "MIDI" if self.midi_transport else "AUDIO"
        if name == "playing":
            self.cuia("TOGGLE_%s_PLAY" % kind)
        elif name == "record":
            self.cuia("TOGGLE_%s_RECORD" % kind)
        elif name == "metronome":
            z = self.zynseq.zctrl_metro_mode
            if on:
                z.set_value(self._metro_mode)
            else:
                self._metro_mode = z.value or METRO_AUTO
                z.set_value(0)
        elif name == "session_record":
            self.cuia("TOGGLE_SESSION_RECORD")

    def stop(self):
        kind = "MIDI" if self.midi_transport else "AUDIO"
        self.cuia("STOP_%s_PLAY" % kind)
        self.cuia("STOP_%s_RECORD" % kind)

    def tap_tempo(self):
        self.cuia("TAP_TEMPO")

    def tempo_param(self):
        return TempoParam(self.zynseq)

    # ---- chains ----

    def bank_tracks(self):
        chains = self.chains
        return [ZynTrack(chains[cid]) for cid in self.chain_ids() if cid != 0 and cid in chains]

    def nav_tracks(self):
        return [ZynTrack(c) for c in self.chains.values()]

    def selected_track(self):
        chain = self.sm.chain_manager.get_active_chain()
        return ZynTrack(chain) if chain is not None else None

    def select_track(self, track):
        self.sm.chain_manager.set_active_chain_by_id(track.key)

    def master_track(self):
        """The main chain (chain 0), or None."""
        main = self.chains.get(0)
        return ZynTrack(main) if main is not None else None

    def master_param(self, name):
        main = self.chains.get(0)
        if name == "volume" and main is not None:
            return ZynTrack(main).param("volume")
        return None

    # ---- zynpad: phrases are the scenes ----

    def can_scene_step(self, delta):
        return 0 <= self.zynseq.phrase + delta < self.zynseq.phrases

    def scene_step(self, delta):
        if self.can_scene_step(delta):
            self.zynseq.select_phrase(self.zynseq.phrase + delta)

    def fire_selected_scene(self):
        z = self.zynseq
        z.libseq.togglePlayState(z.scene, z.phrase, PHRASE_CHANNEL)

    # ---- zynpots, switches ----

    def zynpot(self, index):
        return ZynpotTarget(self.cuia, index)

    def switch_thresholds(self):
        """(bold_us, long_us), Zynthian's switch timing."""
        if self._switch_thresholds is not None:
            return self._switch_thresholds()
        return SWITCH_BOLD_US, SWITCH_LONG_US

    def zynswitch(self, index, kind):
        """kind: 'S', 'B' or 'L' (zynthian_gui cuia_zynswitch)."""
        self.cuia("ZYNSWITCH", [index, kind])

    # ---- clip grid: zynpad's launchers ----

    def grid_rows(self):
        """Rows of the grid: the phrases of the current scene."""
        return self.zynseq.phrases

    def _sequence(self, track, phrase):
        """(midi_chan, the sequence's state dict) for chain `track` in `phrase`, or None."""
        chan = getattr(track.chain, "midi_chan", None)
        if not isinstance(chan, int) or not 0 <= chan < PHRASE_CHANNEL:
            return None             # audio chains have no launcher
        z = self.zynseq
        try:
            info = z.state["scenes"][z.scene]["phrases"][phrase]["sequences"][chan]
        except (AttributeError, KeyError, IndexError, TypeError):
            return None
        return (chan, info) if info else None

    def _filled(self, track, phrase, info):
        """Whether the pad holds anything, as the mixer's launcher draws it: a clippy pad its clip
        file, a sequence its events."""
        if info.get("group", 0) >= SEQ_GROUP_CLIPPY:
            try:
                proc = track.chain.get_clippy_processor()
                return bool(proc.controllers_dict["file %d" % (phrase + 1)].get_value())
            except Exception:
                return False
        return info.get("nEvents", 0) > 0

    def clip_state(self, track, phrase):
        """CLIP_NONE (no sequence there: empty or disabled), CLIP_STOPPED, CLIP_PLAYING or
        CLIP_QUEUED (playing or stopped, waiting for the sync point to change)."""
        if not 0 <= phrase < self.grid_rows():
            return CLIP_NONE
        seq = self._sequence(track, phrase)
        if seq is None:
            return CLIP_NONE
        _chan, info = seq
        if info.get("repeat", 1) == 0 or not self._filled(track, phrase, info):
            return CLIP_NONE
        state = info.get("state", SEQ_STOPPED)
        if state == SEQ_STOPPED:
            return CLIP_STOPPED
        return CLIP_QUEUED if state in SEQ_QUEUED else CLIP_PLAYING

    def clip_press(self, track, phrase):
        """Launch / stop the pad, as zynpad does (returns False when there's no launcher there)."""
        seq = self._sequence(track, phrase) if 0 <= phrase < self.grid_rows() else None
        if seq is None:
            return False
        chan = seq[0]
        if self._pad_press is not None:
            self._pad_press(phrase, chan)
        else:
            z = self.zynseq
            z.libseq.togglePlayState(z.scene, phrase, chan)
        return True

    def clip_release(self, track, phrase):
        seq = self._sequence(track, phrase)
        if seq is not None and self._pad_release is not None:
            self._pad_release(phrase, seq[0])


class _ClipBinding(object):
    """A pad (or button) with role clip_T_S: grid cell (t, s), 1-based, from the scroll position."""

    def __init__(self, component, t, s, led):
        self.component = component
        self.t, self.s = t, s
        self.led = led              # LED index, or None
        self.held = None            # (track, phrase) it pressed, for the release


class ZynMapping(Mapping):
    """mapping.Mapping with Zynthian's differences: stop and record behave like Zynthian's
    transport, device_param_1..4 drive the zynpots, master_mute mutes the main chain, faders
    can jump instead of picking up, displays show the active chain's name (and a `param`
    display the value being moved), clip_T_S pads show and launch zynpad's grid, nav_* drive
    Zynthian's switches and arrows, zynthian.<cuia> runs a CUIA, and roles Zynthian has no use
    for are left unbound (and so logged as unknown)."""

    # False: a fader sets its parameter as soon as it moves (jump). True: it waits until it
    # reaches the parameter's value (pick-up, in the device and in Mapping._analog).
    fader_pickup = True
    host_namespace = HOST_NAMESPACE

    def __init__(self, song, notify, log=None):
        Mapping.__init__(self, song, notify, log)
        self._displays = []         # every display with a name role or `param`
        self._param_displays = []
        self._text_cache = {}
        self._text_hold = 0         # ticks until a param display goes back to the chain name
        self._name_ctx = None       # (selected chain, bank offset) the name was last shown for
        self._clips = {}            # (kind, index) -> _ClipBinding
        self._pads = {}             # pad index -> _ButtonBinding (pads with other roles)
        self._rgb_cache = {}
        self.grid_col = 0           # clip grid scroll position (columns = chains, rows = phrases)
        self.grid_row = 0
        self.shift = False          # the shift modifier is held
        self._switch_down = {}      # zynswitch -> t_ms of the press
        self._event = None          # the EVENT being handled (press timing)

    def bind(self, components):
        Mapping.bind(self, components)
        local = set(c.index for c in components if c.kind == P.KIND_LED and c.local_only)
        self._clips, self._pads = {}, {}
        for c in components:
            if c.kind not in (P.KIND_BUTTON, P.KIND_PAD) or not c.role:
                continue
            led = c.led if c.has_led and c.led not in local else None
            t, s = clip_role(c.role)
            if t is not None:
                self._clips[(c.kind, c.index)] = _ClipBinding(c, t, s, led)
                if c.kind == P.KIND_BUTTON:
                    self._leds.pop(led, None)
                continue
            if c.kind == P.KIND_PAD:
                br = self._button_role(c.role)
                if br is None:
                    self._unknown(c)
                    continue
                b = _ButtonBinding(c, br)
                self._pads[c.index] = b
                if led is not None:
                    self._leds[led] = _LedBinding(led, b.level)
        for led in [cb.led for cb in self._clips.values() if cb.led is not None]:
            self._leds.pop(led, None)       # a clip LED is coloured, not on / off
        # Buttons with clip roles came back from _button_role as unknown: they're bound above.
        self.unknown_roles = [u for u in self.unknown_roles if clip_role(u[2])[0] is None]
        self._displays, self._param_displays = [], []
        for c in components:
            if c.kind != P.KIND_DISPLAY or not c.cols or not c.rows:
                continue
            if c.role in DISPLAY_NAME_ROLES or c.role == DISPLAY_PARAM_ROLE:
                self._displays.append(c)
                if c.role == DISPLAY_PARAM_ROLE:
                    self._param_displays.append(c)
            else:
                self._unknown(c)            # time, status: nothing to show from Zynthian yet

    def forget(self):
        Mapping.forget(self)
        self._text_cache = {}
        self._rgb_cache = {}
        self._name_ctx = None

    def release(self):
        Mapping.release(self)
        for b in self._pads.values():
            b.held = False
        for cb in self._clips.values():
            cb.held = None
        self.shift = False
        self._switch_down = {}

    # ---- events ----

    def handle_event(self, ev):
        self._event = ev
        try:
            if ev.type in (P.EV_BUTTON, P.EV_PAD):
                kind = P.KIND_PAD if ev.type == P.EV_PAD else P.KIND_BUTTON
                cb = self._clips.get((kind, ev.index))
                if cb is not None:
                    self._clip_event(cb, ev.pressed)
                    self.refresh()
                    return
                if ev.type == P.EV_PAD:
                    b = self._pads.get(ev.index)
                    if b is not None:
                        self._button_event(b, ev.pressed)
                    return
            elif ev.type == P.EV_TOUCH:
                return                      # nothing in Zynthian uses touch yet
            Mapping.handle_event(self, ev)
            if ev.type in (P.EV_ANALOG, P.EV_ENCODER):
                table = self._analogs if ev.type == P.EV_ANALOG else self._encoders
                b = table.get(ev.index)
                p = b.target() if b is not None else None
                if p is not None and not isinstance(p, ZynpotTarget):
                    self._show_value(p, b.component.role)
        finally:
            self._event = None

    def _button_event(self, b, down):
        """A pad with a button role: what Mapping.handle_event does for a button."""
        if down:
            b.held, b.held_ticks = True, 0
            if b.role.press is not None:
                b.role.press()
        else:
            b.held = False
            if b.role.release is not None:
                b.role.release()
        self.refresh()

    # ---- role tables ----

    def _button_role(self, role):
        if role in UNSUPPORTED_BUTTON_ROLES:
            return None
        s = self.song
        if role == "stop":
            return _ButtonRole(press=s.stop, momentary=True)
        if role == "master_mute":
            return self._track_flag_role("mute", s.master_track)
        if role == "record":
            # Zynthian records without playing, so no "armed" blink: lit while recording.
            return _ButtonRole(press=lambda: s.set_flag("record", not s.get_flag("record")),
                         state=lambda: ON if s.get_flag("record") else OFF)
        if role in NAV_SWITCHES:
            return self._switch_role(NAV_SWITCHES[role])
        if role in NAV_ARROWS:
            cuia = NAV_ARROWS[role]
            return _ButtonRole(press=lambda: s.cuia(cuia), repeat=lambda: s.cuia(cuia), momentary=True)
        m = _CUIA_ROLE.match(role)
        if m:
            cuia = m.group(1).upper()
            return _ButtonRole(press=lambda: s.cuia(cuia), momentary=True)
        if role == "shift":
            return _ButtonRole(press=lambda: setattr(self, "shift", True),
                               release=lambda: setattr(self, "shift", False), momentary=True)
        if role in GRID_STEPS:
            dc, dr = GRID_STEPS[role]
            return _ButtonRole(press=lambda: self.grid_step(dc, dr),
                               state=lambda: ON if self._can_grid_step(dc, dr) else OFF)
        return Mapping._button_role(self, role)

    def _switch_role(self, index):
        """A Zynthian switch: S / B / L on release, from the time it was held (EVENT t_ms)."""
        def press():
            if self._event is not None:
                self._switch_down[index] = self._event.t_ms

        def release():
            t0 = self._switch_down.pop(index, None)
            if t0 is None or self._event is None:
                return
            bold_us, long_us = self.song.switch_thresholds()
            self.song.zynswitch(index, press_type(P.t_ms_diff(self._event.t_ms, t0), bold_us, long_us))
        return _ButtonRole(press=press, release=release, momentary=True)

    # ---- clip grid ----

    def _grid_size(self):
        """(columns, rows) the bound clip_T_S cells cover (at least 1 x 1)."""
        cols = max([cb.t for cb in self._clips.values()] or [1])
        rows = max([cb.s for cb in self._clips.values()] or [1])
        return cols, rows

    def _grid_max(self):
        """The largest column and row offsets that still show something."""
        cols, rows = self._grid_size()
        return (max(0, len(self.song.bank_tracks()) - cols), max(0, self.song.grid_rows() - rows))

    def _can_grid_step(self, dc, dr):
        max_col, max_row = self._grid_max()
        col, row = self.grid_col + dc, self.grid_row + dr
        return bool(dc or dr) and 0 <= col <= max_col and 0 <= row <= max_row

    def grid_step(self, dc, dr):
        """grid_left / right / up / down: scroll the grid by one, clamped."""
        if self._can_grid_step(dc, dr):
            self.grid_col += dc
            self.grid_row += dr

    def _clamp_grid(self):
        max_col, max_row = self._grid_max()
        self.grid_col = min(max(0, self.grid_col), max_col)
        self.grid_row = min(max(0, self.grid_row), max_row)

    def clip_cell(self, cb, tracks=None):
        """(track, phrase) under clip binding `cb` at the current scroll, or None."""
        tracks = self.song.bank_tracks() if tracks is None else tracks
        col = self.grid_col + cb.t - 1
        phrase = self.grid_row + cb.s - 1
        if not 0 <= col < len(tracks) or not 0 <= phrase < self.song.grid_rows():
            return None
        return tracks[col], phrase

    def _clip_event(self, cb, down):
        if down:
            cell = self.clip_cell(cb)
            if cell is not None and self.song.clip_press(*cell):
                cb.held = cell
        elif cb.held is not None:
            cell, cb.held = cb.held, None       # the pad it pressed, even if the grid scrolled since
            self.song.clip_release(*cell)

    def _refresh_clips(self):
        tracks = None
        for cb in self._clips.values():
            if cb.led is None:
                continue
            if tracks is None:
                tracks = self.song.bank_tracks()
            cell = self.clip_cell(cb, tracks)
            rgb = CLIP_RGB[self.song.clip_state(*cell) if cell is not None else CLIP_NONE]
            if self._rgb_cache.get(cb.led) != rgb:
                self._rgb_cache[cb.led] = rgb
                self.notify(P.FB_LED_RGB, P.fb_led_rgb(cb.led, *rgb))

    # ---- clock and feedback ----

    def tick(self):
        if self._text_hold > 0:
            self._text_hold -= 1        # at 0, the refresh below puts the chain name back
        Mapping.tick(self)

    def refresh(self):
        self._clamp_grid()
        Mapping.refresh(self)
        self._refresh_clips()
        t = self.song.selected_track()
        ctx = (t.key if t is not None else None, self.bank_offset)
        if ctx != self._name_ctx:
            self._name_ctx = ctx
            self._text_hold = 0         # another chain or bank: its name, at once
        name = t.name if t is not None else ""
        for c in self._displays:
            if self._text_hold == 0 or c not in self._param_displays:
                self._show_text(c, name)

    def _show_value(self, p, role):
        """A knob with `role` moved `p`: "<short name> <value>" on the param displays (just the
        value when both don't fit), for TEXT_HOLD_TICKS."""
        if not self._param_displays:
            return
        value = p.display_text() if hasattr(p, "display_text") else "%d" % round(p.get_norm() * 100)
        label = param_label(role)
        for c in self._param_displays:
            both = "%s %s" % (label, value)
            self._show_text(c, both if label and len(both) <= c.cols else value)
        self._text_hold = TEXT_HOLD_TICKS

    def blank_displays(self):
        """Every display blank (sleep). Like all_leds_off, the cache then says blank."""
        for c in self._displays:
            self._show_text(c, "")

    def _show_text(self, c, text):
        line = _display_text(text, c.cols)
        if self._text_cache.get(c.index) != line:
            self._text_cache[c.index] = line
            self.notify(P.FB_TEXT, P.fb_text(c.index, 0, 0, line))

    def _cont_role(self, role):
        if role in UNSUPPORTED_CONT_ROLES:
            return None
        if role == "nav_scroll":
            return lambda: self.song.zynpot(ZYNPOT_SELECT)
        if role.startswith("device_param_"):
            num = role[len("device_param_"):]
            if not num.isdigit() or not 1 <= int(num) <= NUM_ZYNPOTS:
                return None
            index = int(num) - 1
            return lambda: self.song.zynpot(index)
        return Mapping._cont_role(self, role)

    def _analog(self, b, raw):
        p = b.target()
        if isinstance(p, ZynpotTarget) or (p is not None and not self.fader_pickup):
            p.set_norm(float(raw) / V14)    # zynpots: Zynthian's own pick-up handles the jump
            return
        Mapping._analog(self, b, raw)

    def _refresh_analog(self, b):
        # Jump mode sends no pick-up targets, so the device never holds a fader back.
        if not self.fader_pickup or isinstance(b.target(), ZynpotTarget):
            self._value_cache[(P.KIND_ANALOG, b.component.index)] = None
            return
        Mapping._refresh_analog(self, b)

    def _refresh_encoder(self, b):
        c = b.component
        if isinstance(b.target(), ZynpotTarget):
            self._value_cache[(P.KIND_ENCODER, c.index)] = None
            ring = (P.RING_WRAP, 0, False, True)
            if c.ring and self._ring_cache.get(c.index) != ring:
                self._ring_cache[c.index] = ring
                self.notify(P.FB_RING, P.fb_ring(c.index, ring[0], ring[1], ring[2], ring[3]))
            return
        Mapping._refresh_encoder(self, b)

    def all_leds_off(self, components):
        """Every LED the script may drive, off (sleep). The cache then says off, so the next
        refresh() after forget() sends the real state again."""
        leds = [c.index for c in components if c.kind == P.KIND_LED and not c.local_only]
        leds.sort()
        start = 0
        while start < len(leds):
            end = start
            while end + 1 < len(leds) and leds[end + 1] == leds[end] + 1 and end - start < 63:
                end += 1
            first, n = leds[start], end - start + 1
            if n == 1:
                self.notify(P.FB_LED, P.fb_led(first, OFF))
            else:
                self.notify(P.FB_LEDS, P.fb_leds(first, [OFF] * n))
            for i in leds[start:end + 1]:
                self._led_cache[i] = OFF
            start = end + 1
        for cb in self._clips.values():
            if cb.led is not None:
                self._rgb_cache[cb.led] = CLIP_RGB[CLIP_NONE]
