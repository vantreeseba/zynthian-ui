# SPDX-License-Identifier: MIT
# Copied from gemifi (hosts/ableton-live/tests/fakes.py), MIT licensed; see ../LICENSE-MIT.
"""Test doubles: a gemifi device that answers per sysex.md 4.6, and a song for mapping.py."""

from gemifi import protocol as P
from gemifi.mapping import Param, Song, Track


def C(kind, index, role, **kw):
    return P.Component(kind, index, 0, id=kw.pop("id", "%s%d" % (P.KIND_NAMES[kind], index)),
                       name=kw.pop("name", ""), role=role, **kw)


def default_components():
    """A small device using several roles (plus mcu: names and an x_ role)."""
    B, A, E, L = P.KIND_BUTTON, P.KIND_ANALOG, P.KIND_ENCODER, P.KIND_LED
    return [
        C(B, 0, "play", led=0),
        C(B, 1, "stop", led=1),
        C(B, 2, "record", led=2),
        C(B, 3, "bank_right", led=3),        # mcu: name -> bank_next
        C(B, 4, "arm_1", led=4),
        C(B, 5, "rec_2", led=5),             # mcu: name -> arm_2
        C(B, 6, "select_1", led=6),
        C(B, 7, "rewind"),                   # no LED
        C(B, 8, "x_custom"),
        C(A, 0, "volume_1"),
        C(A, 1, "fader_2"),                  # mcu: name -> volume_2
        C(A, 2, "track_volume"),
        C(E, 0, "device_param_1", ring=11),
        C(E, 1, "vpot_1", ring=11),          # mcu: name -> pan_1
        C(E, 2, "tempo", ring=0),
    ] + [
        # LEDs 0..6 belong to the buttons above (no role of their own); 7 and 8 stand alone.
        C(L, i, "", colours=1) for i in range(7)
    ] + [
        C(L, 7, "metronome", colours=2),
        C(L, 8, "", colours=1),
    ]


class FakeDevice(object):
    """Parses what the script sends and answers like firmware with caps bit 6 would."""

    def __init__(self, components=None, caps=P.CAP_CONFIG | P.CAP_EVENT | P.CAP_FEEDBACK, node="test-pad"):
        comps = default_components() if components is None else components
        self.by_kind = {}
        for c in comps:
            self.by_kind.setdefault(c.kind, []).append(c)
        for lst in self.by_kind.values():
            for i, c in enumerate(lst):          # the device numbers them 0..count-1 per kind
                c.index, c.count = i, len(lst)
        self.hello = P.Hello(role=0, board_id=3, fw_version=(2 << 16), proto_version=1,
                             max_chunk=0, caps=caps, uid=bytes(range(12)), node=node)
        self.params = {P.PARAM_DEBUG_EVENTS: 0}
        self.requests = []           # Frame
        self.notifications = []      # Frame
        self.drop = set()            # (cmd, nth request with that cmd): its reply is lost
        self.silent = False          # unplugged: answers nothing
        self._count = {}
        self.out = None              # callable(bytes): into the script
        self.modes_on = set()        # layer modes that are on (EVENT type 8)
        self.replay_layers = True    # SET_PARAM debug.events 1 replays EVENT 8 (firmware with layers)
        self.replay_first = False    # ... ahead of the SET_PARAM reply

    def wire(self, data):
        """The script sent `data`."""
        f = P.parse_frame(bytes(data))
        if P.NOTIFY_FIRST <= f.cmd <= P.NOTIFY_LAST:
            self.notifications.append(f)
            return
        self.requests.append(f)
        n = self._count[f.cmd] = self._count.get(f.cmd, 0) + 1
        if self.silent or (f.cmd, n) in self.drop:
            return
        reply = self.answer(f)
        replay = (f.cmd == P.SET_PARAM and P.dec_u14(f.payload[0:2]) == P.PARAM_DEBUG_EVENTS and
                  P.dec_s32(f.payload[4:9]) == 1 and self.replay_layers and self.out is not None)
        if replay and self.replay_first:
            self._replay_layers()
        if reply is not None and self.out is not None:
            self.out(P.build_frame(f.cmd | P.REPLY, f.seq, reply))
        if replay and not self.replay_first:
            self._replay_layers()

    def _replay_layers(self):
        for m in sorted(self.modes_on):
            self.event(P.EV_LAYER, m, 1)

    def set_mode(self, index, on):
        """A layer turns on or off: EVENT type 8 while debug.events is on."""
        (self.modes_on.add if on else self.modes_on.discard)(index)
        if self.params.get(P.PARAM_DEBUG_EVENTS) == 1 and self.out is not None:
            self.event(P.EV_LAYER, index, 1 if on else 0)

    def answer(self, f):
        if f.cmd == P.HELLO:
            return P.build_hello_reply(self.hello)
        if f.cmd == P.GET_COMPONENT:
            if not self.hello.caps & P.CAP_FEEDBACK:
                return bytes([P.BAD_CMD])
            kind, index = f.payload[0], P.dec_u14(f.payload[1:3])
            lst = self.by_kind.get(kind, [])
            if index >= len(lst):
                return bytes([P.BAD_PARAM])
            return P.build_component_reply(lst[index])
        if f.cmd in (P.GET_PARAM, P.SET_PARAM):
            pid, index = P.dec_u14(f.payload[0:2]), P.dec_u14(f.payload[2:4])
            if pid not in self.params:
                return bytes([P.BAD_PARAM])
            if f.cmd == P.SET_PARAM:
                self.params[pid] = P.dec_s32(f.payload[4:9])
            return P.build_param_reply(pid, index, self.params[pid])
        return bytes([P.BAD_CMD])

    def boot(self):
        """A restart, as the firmware does it: debug.events back to 0, then BOOTED (sysex.md 4.6.3)."""
        self.params[P.PARAM_DEBUG_EVENTS] = 0
        self.notifications = []
        payload = P.build_hello_reply(self.hello)[1:]
        self.out(P.build_frame(P.BOOTED, 5, payload))

    def event(self, etype, index, value, t_ms=1234):
        self.out(P.build_frame(P.EVENT, 0, P.build_event(etype, index, value, t_ms)))

    def take(self, cmd=None):
        """Pop the notifications so far (optionally only `cmd`) as (cmd, payload bytes) pairs."""
        got = [(f.cmd, f.payload) for f in self.notifications if cmd is None or f.cmd == cmd]
        self.notifications = []
        return got

    def count(self, cmd):
        return sum(1 for f in self.requests if f.cmd == cmd)


# ---- a fake song ----

class FakeParam(Param):
    def __init__(self, value=0.0, bipolar=False, name="p"):
        self.value = value
        self.bipolar = bipolar
        self.name = name

    def get_norm(self):
        return self.value

    def set_norm(self, v):
        self.value = min(1.0, max(0.0, v))


class FakeTrack(Track):
    def __init__(self, name, can_arm=True):
        self.name = name
        self.flags = {"mute": False, "solo": False, "arm": False}
        self._can_arm = can_arm
        self.params = {"volume": FakeParam(0.85, name=name + ".vol"),
                       "pan": FakeParam(0.5, bipolar=True, name=name + ".pan"),
                       "send_a": FakeParam(0.0), "send_b": FakeParam(0.0)}

    def get_flag(self, name):
        return self.flags[name]

    def set_flag(self, name, on):
        self.flags[name] = on

    def can_arm(self):
        return self._can_arm

    def param(self, name):
        return self.params.get(name)


class FakeSong(Song):
    def __init__(self, n_tracks=10):
        self.flags = dict((f, False) for f in Song.TRANSPORT_FLAGS)
        self.tracks = [FakeTrack("t%d" % (i + 1)) for i in range(n_tracks)]
        self.selected = self.tracks[0] if self.tracks else None
        self.master = {"volume": FakeParam(0.85), "cue": FakeParam(0.85), "crossfader": FakeParam(0.5, True)}
        self.tempo = FakeParam(0.1)
        self.devices = [[FakeParam(i / 20.0, name="d0.%d" % i) for i in range(12)],
                        [FakeParam(0.5, name="d1.%d" % i) for i in range(4)]]
        self.device_on = [FakeParam(1.0), FakeParam(0.0)]
        self.device_index = 0
        self.scene = 0
        self.n_scenes = 3
        self.jumps = []
        self.calls = []

    def get_flag(self, name):
        return self.flags[name]

    def set_flag(self, name, on):
        self.flags[name] = on

    def tap_tempo(self):
        self.calls.append("tap")

    def undo(self):
        self.calls.append("undo")

    def redo(self):
        self.calls.append("redo")

    def jump(self, beats):
        self.jumps.append(beats)

    def stop_all_clips(self):
        self.calls.append("stop_all")

    def tempo_param(self):
        return self.tempo

    def bank_tracks(self):
        return self.tracks

    def selected_track(self):
        return self.selected

    def select_track(self, t):
        self.selected = t

    def master_param(self, name):
        return self.master.get(name)

    def device_key(self):
        return self.device_index

    def device_params(self):
        return self.devices[self.device_index]

    def device_on_param(self):
        return self.device_on[self.device_index]

    def can_device_step(self, d):
        return 0 <= self.device_index + d < len(self.devices)

    def device_step(self, d):
        if self.can_device_step(d):
            self.device_index += d

    def can_scene_step(self, d):
        return 0 <= self.scene + d < self.n_scenes

    def scene_step(self, d):
        if self.can_scene_step(d):
            self.scene += d

    def fire_selected_scene(self):
        self.calls.append("fire%d" % self.scene)


class FakeCInstance(object):
    """The bits of Live's c_instance the surface uses."""

    def __init__(self, device):
        self.device = device
        self.sent = []
        self.messages = []
        self.logs = []

    def song(self):
        raise AssertionError("tests pass a fake song")

    def send_midi(self, midi):
        assert isinstance(midi, tuple) and all(isinstance(b, int) for b in midi)
        self.sent.append(midi)
        self.device.wire(bytes(midi))

    def show_message(self, text):
        self.messages.append(text)

    def log_message(self, text):
        self.logs.append(text)

    def request_rebuild_midi_map(self):
        pass
