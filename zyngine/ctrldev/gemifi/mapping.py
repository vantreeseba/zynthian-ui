# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Benjamin Van Treese
# Copied from gemifi (hosts/ableton-live/gemifi/mapping.py), MIT licensed (with strip_prev/next and
# <host>.<command> roles, gemifi issue #21; the per-refresh read cache and the deferred encoder
# FB_VALUE from gemifi 98d560f; FB_RING sent only when its lit LEDs change, daw-scripts.md 5.12).
# See LICENSE-MIT in this directory. The rest of the gemifi driver is GPL.
"""Roles -> behaviour (hosts/README.md, "Roles").

Pure Python, no Live imports. The mapping talks to an abstract song (`Song`, `Track`, `Param`
below); live_song.py adapts the real Live Object Model to it and the tests use a fake one.

Inputs: EVENTs from the device (protocol.Event), by component index.
Outputs: FB_* notifications through `notify(cmd, payload)`.

State is polled, not listened to: refresh() (called every tick, ~100 ms) reads every bound
state, compares it with what was last sent, and sends only the differences. That keeps the
script free of per-track / per-parameter listener bookkeeping, and a lost notification is
fixed by the next change (sysex.md 4.6.2 says feedback is idempotent).

Output the device can't show is left out (daw-scripts.md 5.12): an encoder EVENT moves the ring
at once but leaves FB_VALUE to the next refresh, and FB_RING goes out only when the LEDs it
lights change. `on_dirty()`, when set, is called when something waits for the next refresh.
"""

from . import protocol as P

STRIPS = 8                    # mixer strips and device params per bank
V14 = P.U14_MAX               # 16383
ON, OFF, BLINK = 127, 0, 1    # FB_LED levels (sysex.md 4.6.2: 0 off, 1 blink, 2..127 on)
ENCODER_STEPS = 128           # encoder units for a parameter's full range
PICKUP_WINDOW = V14 * 3 // 100  # an analog within 3 % of the target picks it up
JUMP_BEATS = 4.0              # rewind / fast_forward step
REPEAT_DELAY_TICKS = 3        # held rewind / fast_forward repeats after ~300 ms, then every tick
HOST_NAMESPACE = "live."       # this script's <host>.<command> roles (none yet); other hosts' are ignored


def to14(norm):
    if norm <= 0.0:
        return 0
    if norm >= 1.0:
        return V14
    return int(round(norm * V14))


def ring_lit(ring, mode, value):
    """What an encoder's `ring` LEDs show for `value`, as the firmware draws them
    (daw-scripts.md 5.12): the position p for dot, boost/cut and wrap, the half-width w for
    spread. Two values with the same result light the same LEDs."""
    if mode == P.RING_SPREAD:
        return value * (ring // 2) // V14
    return (value * max(0, ring - 1) + 8191) // V14


# ---------------------------------------------------------------------------
# The abstract song. Subclass and override; the defaults make missing features harmless.
# ---------------------------------------------------------------------------

class Param(object):
    """A continuous target. `key` identifies the underlying parameter (so the mapping notices
    when a control's target changes, e.g. after a bank switch)."""

    bipolar = False           # centre = neutral (pan): ring shows boost/cut with the centre LED

    @property
    def key(self):
        return id(self)

    def get_norm(self):
        raise NotImplementedError

    def set_norm(self, value):
        raise NotImplementedError

    def nudge(self, delta):
        """Encoder turn by `delta` units (signed)."""
        v = self.get_norm() + float(delta) / ENCODER_STEPS
        self.set_norm(min(1.0, max(0.0, v)))


class Track(object):
    @property
    def key(self):
        return id(self)

    def same(self, other):
        return other is not None and self.key == other.key

    def get_flag(self, name):
        """name: 'mute', 'solo', 'arm'."""
        return False

    def set_flag(self, name, on):
        pass

    def can_arm(self):
        return False

    def param(self, name):
        """name: 'volume', 'pan', 'send_a', 'send_b'. Returns a Param or None."""
        return None


class Song(object):
    TRANSPORT_FLAGS = ("playing", "record", "loop", "metronome", "overdub", "session_record",
                       "automation_arm")

    # transport
    def get_flag(self, name):
        return False

    def set_flag(self, name, on):
        pass

    def tap_tempo(self):
        pass

    def undo(self):
        pass

    def redo(self):
        pass

    def jump(self, beats):
        pass

    def stop_all_clips(self):
        pass

    def tempo_param(self):
        return None

    # tracks
    def bank_tracks(self):
        """The tracks the 8 mixer strips page through."""
        return []

    def nav_tracks(self):
        """The tracks track_prev / track_next step through (in Live: also returns and master)."""
        return self.bank_tracks()

    def selected_track(self):
        return None

    def select_track(self, track):
        pass

    def master_param(self, name):
        """name: 'volume', 'cue', 'crossfader'."""
        return None

    # selected device
    def device_key(self):
        """Identifies the selected device (None = none)."""
        return None

    def device_params(self):
        """The selected device's parameters, without its on/off switch."""
        return []

    def device_on_param(self):
        return None

    def can_device_step(self, delta):
        return False

    def device_step(self, delta):
        pass

    # session
    def can_scene_step(self, delta):
        return False

    def scene_step(self, delta):
        pass

    def fire_selected_scene(self):
        pass


class _Reads(object):
    """The song as the bindings see it. Inside refresh() each read in CACHED is done once and
    kept until the refresh ends: many bindings ask for the same tracks or device parameters.
    Everything else, and every read outside refresh(), goes straight to the song."""

    CACHED = ("bank_tracks", "nav_tracks", "selected_track", "device_key", "device_params",
              "device_on_param", "can_device_step", "can_scene_step")

    def __init__(self, song, cached=CACHED):
        self._song = song
        self._cached = frozenset(cached)
        self._memo = None

    def begin(self):
        self._memo = {}

    def end(self):
        self._memo = None

    def __getattr__(self, name):
        attr = getattr(self._song, name)
        if name not in self._cached:
            return attr

        def read(*args):
            memo = self._memo
            if memo is None:
                return attr(*args)
            key = (name,) + args
            try:
                return memo[key]
            except KeyError:
                value = memo[key] = attr(*args)
                return value
        return read


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------

class _ButtonRole(object):
    """press() / release() actions and state() -> LED level."""

    def __init__(self, press=None, release=None, state=None, momentary=False, repeat=None):
        self.press = press
        self.release = release
        self.state = state
        self.momentary = momentary      # LED lit while held
        self.repeat = repeat            # called every tick while held, after a delay


class _ButtonBinding(object):
    def __init__(self, component, role):
        self.component = component
        self.role = role
        self.held = False
        self.held_ticks = 0

    def level(self):
        if self.role.momentary:
            return ON if self.held else OFF
        return self.role.state() if self.role.state is not None else OFF


class _LedBinding(object):
    def __init__(self, led, level_fn):
        self.led = led
        self.level = level_fn


class _ContBinding(object):
    def __init__(self, component, target):
        self.component = component
        self.target = target            # () -> Param or None
        self.picked = False
        self.last_raw = None


def _strip_role(role, prefixes):
    """'volume_3' -> ('volume', 3) if the prefix is in `prefixes` and 1 <= N <= 8."""
    base, sep, num = role.rpartition("_")
    if not sep or base not in prefixes or not num.isdigit():
        return None, None
    n = int(num)
    if not 1 <= n <= STRIPS:
        return None, None
    return base, n


class Mapping(object):
    CACHED_READS = _Reads.CACHED

    def __init__(self, song, notify, log=None):
        self.song = song
        self._reads = _Reads(song, self.CACHED_READS)   # what bindings read through
        self.notify = notify
        self.log = log
        self.on_dirty = None          # () -> None: output waits for the next refresh
        self.bank_offset = 0
        self.device_bank = 0
        self._device_key = None
        self._buttons = {}
        self._leds = {}
        self._analogs = {}
        self._encoders = {}
        self._led_cache = {}
        self._value_cache = {}
        self._ring_cache = {}
        self.unknown_roles = []

    # ---- setup ----

    def bind(self, components):
        """Build the bindings from the device's component list (roles already normalised).
        LEDs with owner: local ignore FB_LED, so they get no binding (sysex.md 4.6.1 `owner`)."""
        self._buttons, self._leds, self._analogs, self._encoders = {}, {}, {}, {}
        self.unknown_roles = []
        self.forget()
        local = set(c.index for c in components if c.kind == P.KIND_LED and c.local_only)
        for c in components:
            role = c.role
            if not role:
                continue
            if c.kind == P.KIND_BUTTON:
                br = self._button_role(role)
                if br is None:
                    self._unknown(c)
                    continue
                b = _ButtonBinding(c, br)
                self._buttons[c.index] = b
                if c.has_led and c.led not in local:
                    self._leds[c.led] = _LedBinding(c.led, b.level)
            elif c.kind == P.KIND_LED:
                if c.local_only:
                    continue
                br = self._button_role(role)
                if br is None:
                    self._unknown(c)
                    continue
                if c.index not in self._leds:      # a button's own binding wins (it knows "held")
                    self._leds[c.index] = _LedBinding(c.index, (lambda r: (lambda: OFF if r.momentary or
                                                       r.state is None else r.state()))(br))
            elif c.kind in (P.KIND_ANALOG, P.KIND_ENCODER):
                target = self._cont_role(role)
                if target is None:
                    self._unknown(c)
                    continue
                table = self._analogs if c.kind == P.KIND_ANALOG else self._encoders
                table[c.index] = _ContBinding(c, target)
            # keys and displays: no roles in hosts/README.md yet

    host_namespace = HOST_NAMESPACE

    def _unknown(self, c):
        # x_ roles and another host's commands (<host>.<command>, daw-scripts.md 6): ignored quietly.
        if c.role.startswith("x_") or ("." in c.role and not c.role.startswith(self.host_namespace)):
            return
        self.unknown_roles.append((c.kind, c.index, c.role))

    def forget(self):
        """Drop what was sent: the next refresh() sends everything (after ready / a release)."""
        self._led_cache, self._value_cache, self._ring_cache = {}, {}, {}
        for b in list(self._analogs.values()):
            b.picked, b.last_raw = False, None

    def release(self):
        """The device is being handed back (FB_RELEASE goes out from device.release())."""
        self.forget()
        for b in self._buttons.values():
            b.held = False

    # ---- role tables ----

    def _button_role(self, role):
        s = self._reads
        flag_roles = {"record": "record", "loop": "loop", "metronome": "metronome", "overdub": "overdub",
                      "session_record": "session_record", "automation_arm": "automation_arm"}
        if role == "play":
            return _ButtonRole(press=lambda: s.set_flag("playing", not s.get_flag("playing")),
                               state=lambda: ON if s.get_flag("playing") else OFF)
        if role == "stop":
            return _ButtonRole(press=lambda: s.set_flag("playing", False), momentary=True)
        if role == "record":
            def rec_state():
                if not s.get_flag("record"):
                    return OFF
                return ON if s.get_flag("playing") else BLINK     # armed, waiting for play
            return _ButtonRole(press=lambda: s.set_flag("record", not s.get_flag("record")), state=rec_state)
        if role in flag_roles:
            name = flag_roles[role]
            return _ButtonRole(press=lambda: s.set_flag(name, not s.get_flag(name)),
                               state=lambda: ON if s.get_flag(name) else OFF)
        if role == "tap_tempo":
            return _ButtonRole(press=s.tap_tempo, momentary=True)
        if role == "undo":
            return _ButtonRole(press=s.undo, momentary=True)
        if role == "redo":
            return _ButtonRole(press=s.redo, momentary=True)
        if role in ("rewind", "fast_forward"):
            beats = -JUMP_BEATS if role == "rewind" else JUMP_BEATS
            return _ButtonRole(press=lambda: s.jump(beats), repeat=lambda: s.jump(beats), momentary=True)

        # selected track
        if role in ("track_prev", "track_next"):
            d = -1 if role == "track_prev" else 1
            return _ButtonRole(press=lambda: self._track_step(d),
                               state=lambda: ON if self._can_track_step(d) else OFF)
        if role in ("track_mute", "track_solo", "track_arm"):
            return self._track_flag_role(role[len("track_"):], s.selected_track)

        # mixer strips
        if role in ("bank_prev", "bank_next"):
            d = -1 if role == "bank_prev" else 1
            return _ButtonRole(press=lambda: self.bank_step(d),
                               state=lambda: ON if self._can_bank_step(d) else OFF)
        if role in ("strip_prev", "strip_next"):
            d = -1 if role == "strip_prev" else 1
            return _ButtonRole(press=lambda: self.bank_step(d, 1),
                               state=lambda: ON if self._can_bank_step(d) else OFF)
        base, n = _strip_role(role, ("mute", "solo", "arm", "select"))
        if base is not None:
            strip = (lambda i: (lambda: self.strip_track(i)))(n - 1)
            if base == "select":
                def sel_press():
                    t = strip()
                    if t is not None:
                        s.select_track(t)

                def sel_state():
                    t, cur = strip(), s.selected_track()
                    return ON if t is not None and t.same(cur) else OFF
                return _ButtonRole(press=sel_press, state=sel_state)
            return self._track_flag_role(base, strip)

        # selected device
        if role in ("device_prev", "device_next"):
            d = -1 if role == "device_prev" else 1
            return _ButtonRole(press=lambda: s.device_step(d), state=lambda: ON if s.can_device_step(d) else OFF)
        if role in ("device_bank_prev", "device_bank_next"):
            d = -1 if role == "device_bank_prev" else 1
            return _ButtonRole(press=lambda: self.device_bank_step(d),
                               state=lambda: ON if self._can_device_bank_step(d) else OFF)
        if role == "device_on":
            def on_press():
                p = s.device_on_param()
                if p is not None:
                    p.set_norm(0.0 if p.get_norm() >= 0.5 else 1.0)

            def on_state():
                p = s.device_on_param()
                return ON if p is not None and p.get_norm() >= 0.5 else OFF
            return _ButtonRole(press=on_press, state=on_state)

        # session
        if role == "scene_launch":
            return _ButtonRole(press=s.fire_selected_scene, momentary=True)
        if role in ("scene_prev", "scene_next"):
            d = -1 if role == "scene_prev" else 1
            return _ButtonRole(press=lambda: s.scene_step(d), state=lambda: ON if s.can_scene_step(d) else OFF)
        if role == "stop_all_clips":
            return _ButtonRole(press=s.stop_all_clips, momentary=True)
        return None

    def _track_flag_role(self, name, track_fn):
        def press():
            t = track_fn()
            if t is None or (name == "arm" and not t.can_arm()):
                return
            t.set_flag(name, not t.get_flag(name))

        def state():
            t = track_fn()
            return ON if t is not None and t.get_flag(name) else OFF
        return _ButtonRole(press=press, state=state)

    def _cont_role(self, role):
        """role -> () -> Param or None."""
        s = self._reads
        sel = {"track_volume": "volume", "track_pan": "pan", "send_a": "send_a", "send_b": "send_b"}
        if role in sel:
            name = sel[role]

            def target():
                t = s.selected_track()
                return t.param(name) if t is not None else None
            return target
        master = {"master_volume": "volume", "cue_volume": "cue", "crossfader": "crossfader"}
        if role in master:
            return (lambda name: (lambda: s.master_param(name)))(master[role])
        if role == "tempo":
            return s.tempo_param
        base, n = _strip_role(role, ("volume", "pan"))
        if base is not None:
            def strip_target(i=n - 1, name=base):
                t = self.strip_track(i)
                return t.param(name) if t is not None else None
            return strip_target
        base, n = _strip_role(role, ("device_param",))
        if base is not None:
            def dev_target(i=n - 1):
                params = s.device_params()
                k = self.device_bank * STRIPS + i
                return params[k] if 0 <= k < len(params) else None
            return dev_target
        return None

    # ---- navigation ----

    def strip_track(self, i):
        tracks = self._reads.bank_tracks()
        k = self.bank_offset + i
        return tracks[k] if 0 <= k < len(tracks) else None

    def _can_bank_step(self, d):
        n = len(self._reads.bank_tracks())
        return self.bank_offset > 0 if d < 0 else self.bank_offset + STRIPS < n

    def bank_step(self, d, by=STRIPS):
        """Move the strips by `by` tracks (bank_prev/next: 8, strip_prev/next: 1)."""
        if self._can_bank_step(d):
            self.bank_offset = max(0, self.bank_offset + d * by)

    def _device_bank_count(self):
        n = len(self._reads.device_params())
        return (n + STRIPS - 1) // STRIPS

    def _can_device_bank_step(self, d):
        return self.device_bank > 0 if d < 0 else self.device_bank + 1 < self._device_bank_count()

    def device_bank_step(self, d):
        if self._can_device_bank_step(d):
            self.device_bank += d

    def _track_index(self):
        cur = self._reads.selected_track()
        if cur is None:
            return None, []
        tracks = self._reads.nav_tracks()
        for i, t in enumerate(tracks):
            if t.same(cur):
                return i, tracks
        return None, tracks

    def _can_track_step(self, d):
        i, tracks = self._track_index()
        return i is not None and 0 <= i + d < len(tracks)

    def _track_step(self, d):
        i, tracks = self._track_index()
        if i is not None and 0 <= i + d < len(tracks):
            self.song.select_track(tracks[i + d])

    # ---- events ----

    def handle_event(self, ev):
        if ev.type == P.KIND_BUTTON:
            b = self._buttons.get(ev.index)
            if b is None:
                return
            if ev.value:
                b.held, b.held_ticks = True, 0
                if b.role.press is not None:
                    b.role.press()
            else:
                b.held = False
                if b.role.release is not None:
                    b.role.release()
            self.refresh()
        elif ev.type == P.KIND_ANALOG:
            b = self._analogs.get(ev.index)
            if b is not None:
                self._analog(b, min(V14, ev.value))
        elif ev.type == P.KIND_ENCODER:
            b = self._encoders.get(ev.index)
            if b is not None:
                p = b.target()
                if p is not None and ev.delta:
                    p.nudge(ev.delta)       # the whole delta: accelerated turns are already scaled
                    # The ring follows at once; FB_VALUE waits for the next refresh, so a fast
                    # turn isn't echoed back one FB_VALUE per EVENT.
                    self._refresh_encoder(b, value=False)
                    self._dirty()
        # keys (type 2) and reserved types: not mapped

    def _analog(self, b, raw):
        p = b.target()
        if p is None:
            return
        cur = to14(p.get_norm())
        if not b.picked:
            crossed = b.last_raw is not None and (b.last_raw - cur) * (raw - cur) <= 0
            if crossed or abs(raw - cur) <= PICKUP_WINDOW:
                b.picked = True
        b.last_raw = raw
        if not b.picked:
            return
        p.set_norm(float(raw) / V14)
        # What the knob set is not news to the knob: no FB_VALUE echo.
        self._value_cache[(P.KIND_ANALOG, b.component.index)] = (p.key, to14(p.get_norm()))

    # ---- clock ----

    def tick(self):
        for b in self._buttons.values():
            if b.held and b.role.repeat is not None:
                b.held_ticks += 1
                if b.held_ticks >= REPEAT_DELAY_TICKS:
                    b.role.repeat()
        self.refresh()

    # ---- feedback ----

    def _dirty(self):
        if self.on_dirty is not None:
            self.on_dirty()

    def refresh(self):
        """Send what changed. Every read the bindings make is done once per refresh."""
        self._reads.begin()
        try:
            self._refresh()
        finally:
            self._reads.end()

    def _refresh(self):
        s = self._reads
        dk = s.device_key()
        if dk != self._device_key:
            self._device_key = dk
            self.device_bank = 0
        n = self._device_bank_count()
        if self.device_bank >= max(1, n):
            self.device_bank = max(0, n - 1)
        n = len(s.bank_tracks())
        if self.bank_offset and self.bank_offset >= n:
            self.bank_offset = max(0, ((n - 1) // STRIPS) * STRIPS)
        self._refresh_leds()
        for b in self._analogs.values():
            self._refresh_analog(b)
        for b in self._encoders.values():
            self._refresh_encoder(b)

    def _refresh_leds(self):
        changed = {}
        for led, lb in self._leds.items():
            level = lb.level()
            if self._led_cache.get(led) != level:
                changed[led] = level
                self._led_cache[led] = level
        if not changed:
            return
        idx = sorted(changed)
        run = [idx[0]]
        for i in idx[1:] + [None]:
            if i is not None and i == run[-1] + 1 and len(run) < 64:
                run.append(i)
                continue
            if len(run) == 1:
                self.notify(P.FB_LED, P.fb_led(run[0], changed[run[0]]))
            else:
                self.notify(P.FB_LEDS, P.fb_leds(run[0], [changed[k] for k in run]))
            if i is not None:
                run = [i]

    def _refresh_analog(self, b):
        p = b.target()
        k = (P.KIND_ANALOG, b.component.index)
        if p is None:
            self._value_cache[k] = None
            return
        now = (p.key, to14(p.get_norm()))
        if self._value_cache.get(k) != now:
            self._value_cache[k] = now
            b.picked = False                    # the target moved away from the knob
            b.last_raw = None
            if b.component.takes_value:         # takeover: none ignores FB_VALUE (sysex.md 4.6.2)
                self.notify(P.FB_VALUE, P.fb_value(P.KIND_ANALOG, b.component.index, now[1]))

    def _refresh_encoder(self, b, value=True):
        """value=False: the ring only (FB_VALUE and its cache are left for the next refresh)."""
        c = b.component
        if not value and not c.ring:
            return
        p = b.target()
        k = (P.KIND_ENCODER, c.index)
        if p is None:
            if value:
                self._value_cache[k] = None
            self._send_ring(c, P.RING_WRAP, 0, False, True)     # nothing mapped: ring off
            return
        v = to14(p.get_norm())
        now = (p.key, v)
        if value and self._value_cache.get(k) != now:
            self._value_cache[k] = now
            if c.takes_value:                   # a relative encoder ignores FB_VALUE (sysex.md 4.6.2)
                self.notify(P.FB_VALUE, P.fb_value(P.KIND_ENCODER, c.index, v))
        if p.bipolar:
            self._send_ring(c, P.RING_BOOST_CUT, v, True, False)
        else:
            self._send_ring(c, P.RING_WRAP, v, False, False)

    def _send_ring(self, c, mode, value, centre, off):
        """FB_RING to encoder `c`, only when the LEDs it lights would change."""
        if not c.ring:
            return
        shown = (mode, 0 if off else ring_lit(c.ring, mode, value), centre, off)
        if self._ring_cache.get(c.index) != shown:
            self._ring_cache[c.index] = shown
            self.notify(P.FB_RING, P.fb_ring(c.index, mode, value, centre, off))
