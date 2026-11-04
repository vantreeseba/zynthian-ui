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

No Zynthian imports: everything comes in through the state manager the driver passes, so the
tests can use fakes. Chains are the tracks, the chain mixer is the mixer, zynseq phrases are
the scenes and Zynthian's four zynpots are device_param_1..4.
"""

import unicodedata

from . import protocol as P
from .mapping import Mapping, Param, Song, Track, V14, OFF, ON, _ButtonRole

PHRASE_CHANNEL = 32         # zynseq's phrase launcher "channel" (zynseq.PHRASE_CHANNEL)
NUM_ZYNPOTS = 4
METRO_AUTO = 1              # zynseq metronome mode: click while the transport rolls
TEMPO_MIN, TEMPO_MAX = 10.0, 500.0

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

    def __init__(self, state_manager, chain_ids, midi_transport=False):
        self.sm = state_manager
        self.chain_ids = chain_ids
        self.midi_transport = midi_transport
        self._metro_mode = METRO_AUTO   # the mode metronome "on" goes back to

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

    # ---- zynpots ----

    def zynpot(self, index):
        return ZynpotTarget(self.cuia, index)


class ZynMapping(Mapping):
    """mapping.Mapping with Zynthian's differences: stop and record behave like Zynthian's
    transport, device_param_1..4 drive the zynpots, master_mute mutes the main chain, faders
    can jump instead of picking up, every display shows the active chain's name, and roles
    Zynthian has no use for are left unbound (and so logged as unknown)."""

    # False: a fader sets its parameter as soon as it moves (jump). True: it waits until it
    # reaches the parameter's value (pick-up, in the device and in Mapping._analog).
    fader_pickup = True

    def __init__(self, song, notify, log=None):
        Mapping.__init__(self, song, notify, log)
        self._displays = []
        self._text_cache = {}

    def bind(self, components):
        Mapping.bind(self, components)
        # Displays have no roles yet (hosts/README.md): each one shows the active chain.
        self._displays = [c for c in components if c.kind == P.KIND_DISPLAY and c.cols and c.rows]

    def forget(self):
        Mapping.forget(self)
        self._text_cache = {}

    def refresh(self):
        Mapping.refresh(self)
        t = self.song.selected_track()
        name = t.name if t is not None else ""
        for c in self._displays:
            self._show_text(c, name)

    def blank_displays(self):
        """Every display blank (sleep). Like all_leds_off, the cache then says blank."""
        for c in self._displays:
            self._show_text(c, "")

    def _show_text(self, c, text):
        line = _display_text(text, c.cols)
        if self._text_cache.get(c.index) != line:
            self._text_cache[c.index] = line
            self.notify(P.FB_TEXT, P.fb_text(c.index, 0, 0, line))

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
        return Mapping._button_role(self, role)

    def _cont_role(self, role):
        if role in UNSUPPORTED_CONT_ROLES:
            return None
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
