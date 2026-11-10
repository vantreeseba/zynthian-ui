# SPDX-License-Identifier: MIT
# Copied from gemifi (hosts/ableton-live/tests/test_device.py), MIT licensed; see ../LICENSE-MIT.
from gemifi import protocol as P
from gemifi import device as D

from fakes import FakeDevice, default_components


def connect(fake, **kw):
    dev = D.Device(fake.wire, **kw)
    fake.out = dev.receive
    log = []
    ready = []
    lost = []
    events = []
    dev.on_log = log.append
    dev.on_ready = ready.append
    dev.on_lost = lost.append
    dev.on_event = events.append
    dev.log, dev.ready_calls, dev.lost_calls, dev.events = log, ready, lost, events
    return dev


def test_normalise_role():
    n = D.normalise_role
    assert n("fader_3") == "volume_3"
    assert n("master") == "master_volume"
    assert n("vpot_8") == "pan_8"
    assert n("rec_1") == "arm_1"
    assert (n("bank_left"), n("bank_right")) == ("bank_prev", "bank_next")
    assert (n("channel_left"), n("channel_right")) == ("strip_prev", "strip_next")
    assert (n("cycle"), n("click")) == ("loop", "metronome")
    for same in ("solo_2", "mute_4", "select_1", "play", "stop", "record", "rewind", "fast_forward", "undo",
                 "device_param_3", "x_mine", ""):
        assert n(same) == same
    assert n("fader_x") == "fader_x"
    assert n(" Play ") == "play"


def test_reaches_ready():
    fake = FakeDevice()
    dev = connect(fake)
    dev.start()
    assert dev.state == D.READY
    assert dev.ready_calls == [dev]
    assert fake.params[P.PARAM_DEBUG_EVENTS] == 1
    assert dev.events_before == 0
    comps = dev.all_components()
    assert len(comps) == len(default_components())
    roles = dict(((c.kind, c.index), c.role) for c in comps)
    assert roles[(P.KIND_BUTTON, 3)] == "bank_next"          # mcu: name mapped
    assert roles[(P.KIND_ANALOG, 1)] == "volume_2"
    assert roles[(P.KIND_ENCODER, 1)] == "pan_1"
    assert dev.components[P.KIND_KEYS] == [] and dev.components[P.KIND_DISPLAY] == []
    # Request order: HELLO, then each kind from index 0, then GET/SET debug.events.
    cmds = [f.cmd for f in fake.requests]
    assert cmds[0] == P.HELLO
    assert cmds[-2:] == [P.GET_PARAM, P.SET_PARAM]
    kinds = [f.payload[0] for f in fake.requests if f.cmd == P.GET_COMPONENT]
    assert kinds == sorted(kinds)
    for kind in P.ALL_KINDS:                                  # each kind asked from index 0
        assert any(f.payload[0] == kind and P.dec_u14(f.payload[1:3]) == 0
                   for f in fake.requests if f.cmd == P.GET_COMPONENT)
    # BAD_PARAM for index 0 of kinds 2 and 6 = none; not asked past count for the others.
    n_requests = sum(len(fake.by_kind.get(k, [])) or 1 for k in P.ALL_KINDS)
    assert fake.count(P.GET_COMPONENT) == n_requests
    assert fake.notifications == []                           # nothing sent unasked
    seqs = [f.seq for f in fake.requests]
    assert len(set(seqs)) == len(seqs)                       # a new seq per request


def test_lost_reply_is_resent_with_same_seq():
    fake = FakeDevice()
    fake.drop = {(P.GET_COMPONENT, 3), (P.HELLO, 1)}
    dev = connect(fake)
    dev.start()
    assert dev.state == D.SEARCHING
    for _ in range(D.TIMEOUT_TICKS - 1):
        dev.tick()
    assert fake.count(P.HELLO) == 1                           # not yet timed out
    dev.tick()
    assert fake.count(P.HELLO) == 2
    assert fake.requests[0].seq == fake.requests[1].seq       # same seq on a resend
    assert dev.state == D.ENUMERATING                         # stuck behind the lost GET_COMPONENT
    gc = [f for f in fake.requests if f.cmd == P.GET_COMPONENT]
    assert len(gc) == 3
    for _ in range(D.TIMEOUT_TICKS):
        dev.tick()
    assert dev.state == D.READY
    gc = [f for f in fake.requests if f.cmd == P.GET_COMPONENT]
    assert gc[3].seq == gc[2].seq and gc[3].payload == gc[2].payload
    assert len(dev.all_components()) == len(default_components())


def test_busy_reply_is_resent():
    fake = FakeDevice()
    real = fake.answer
    state = {"busy": 2}

    def answer(f):
        if f.cmd == P.SET_PARAM and state["busy"]:
            state["busy"] -= 1
            return bytes([P.BUSY])
        return real(f)
    fake.answer = answer
    dev = connect(fake)
    dev.start()
    assert dev.state == D.READY
    sets = [f for f in fake.requests if f.cmd == P.SET_PARAM]
    assert len(sets) == 3 and len(set(f.seq for f in sets)) == 1


def test_unknown_or_locked_status_is_logged_not_raised():
    for status in (P.LOCKED, 99):
        fake = FakeDevice()
        real = fake.answer
        fake.answer = lambda f, real=real, status=status: bytes([status]) if f.cmd == P.SET_PARAM else real(f)
        dev = connect(fake)
        dev.start()
        assert dev.state == D.UNSUPPORTED
        assert dev.log[-1] == "SET_PARAM debug.events -> %s" % P.status_name(status)


def test_raw_and_unlocked_are_ignored():
    fake = FakeDevice()
    dev = connect(fake)
    dev.start()
    assert dev.ready
    for cmd, payload in ((P.RAW, bytes([3]) + P.u14(0) + bytes([1]) + P.u14(1234)), (P.UNLOCKED, bytes([1]))):
        assert dev.receive(P.build_frame(cmd, 0, payload))
    assert dev.ready and not dev.events and not dev.lost_calls


def test_no_device_gives_up_then_rescans():
    fake = FakeDevice()
    fake.silent = True
    dev = connect(fake)
    dev.start()
    for _ in range(D.TIMEOUT_TICKS * D.MAX_TRIES):
        dev.tick()
    assert fake.count(P.HELLO) == D.MAX_TRIES
    assert dev.state == D.SEARCHING
    fake.silent = False
    for _ in range(D.RESCAN_TICKS):
        dev.tick()
    assert dev.state == D.READY


def test_device_without_feedback_is_left_alone():
    fake = FakeDevice(caps=P.CAP_CONFIG | P.CAP_EVENT)
    dev = connect(fake)
    dev.start()
    assert dev.state == D.UNSUPPORTED
    assert [f.cmd for f in fake.requests] == [P.HELLO]
    assert not dev.notify(P.FB_LED, P.fb_led(0, 127))       # never notify without caps bit 6
    dev.release()
    assert fake.notifications == []


def test_events_only_when_ready():
    fake = FakeDevice()
    dev = connect(fake)
    fake.out(P.build_frame(P.EVENT, 0, P.build_event(1, 0, 1)))   # before HELLO
    assert dev.events == []
    dev.start()
    fake.event(4, 2, P.int_to_s14(-3))
    assert len(dev.events) == 1
    ev = dev.events[0]
    assert (ev.type, ev.index, ev.delta) == (4, 2, -3)


def test_stray_replies_ignored():
    fake = FakeDevice()
    dev = connect(fake)
    fake.drop = {(P.HELLO, 1)}
    dev.start()
    fake.out(P.build_frame(P.HELLO | P.REPLY, 99, P.build_hello_reply(fake.hello)))   # wrong seq
    assert dev.state == D.SEARCHING
    assert not dev.receive((0xF0, 0x7E, 0x7F, 0x06, 0x02, 0xF7))                  # not ours


def test_fragmented_sysex():
    fake = FakeDevice()
    dev = connect(fake)
    fake.out = None
    dev.start()
    reply = P.build_frame(P.HELLO | P.REPLY, fake.requests[0].seq, P.build_hello_reply(fake.hello))
    fake.out = dev.receive
    dev.receive(reply[:10])
    assert dev.state == D.SEARCHING
    dev.receive(reply[10:])
    assert dev.state == D.READY


def test_keepalive_notices_a_reboot():
    fake = FakeDevice()
    dev = connect(fake, keepalive_ticks=5)
    dev.start()
    for _ in range(5):
        dev.tick()
    assert dev.state == D.READY and dev.lost_calls == []
    fake.params[P.PARAM_DEBUG_EVENTS] = 0          # the device restarted with events off
    for _ in range(5):
        dev.tick()
    assert dev.lost_calls == [dev]
    assert dev.state == D.READY and len(dev.ready_calls) == 2     # found again, enumerated again
    assert fake.params[P.PARAM_DEBUG_EVENTS] == 1


def test_keepalive_timeout_means_lost():
    fake = FakeDevice()
    dev = connect(fake, keepalive_ticks=2)
    dev.start()
    fake.silent = True
    for _ in range(2 + D.TIMEOUT_TICKS * D.MAX_TRIES):
        dev.tick()
    assert dev.lost_calls == [dev]
    assert dev.state == D.SEARCHING


def test_release_restores_debug_events():
    fake = FakeDevice()
    dev = connect(fake)
    dev.start()
    fake.notifications = []
    dev.release()
    assert [(f.cmd, f.payload) for f in fake.notifications] == [(P.FB_RELEASE, bytes([0]))]
    assert fake.params[P.PARAM_DEBUG_EVENTS] == 0
    assert dev.state == D.IDLE


def test_booted_resyncs():
    """sysex.md 4.6.3: BOOTED means the device restarted; start again and re-send everything."""
    fake = FakeDevice()
    dev = connect(fake)
    dev.start()
    assert dev.state == D.READY and fake.params[P.PARAM_DEBUG_EVENTS] == 1
    hellos = fake.count(P.HELLO)
    fake.boot()
    assert dev.lost_calls == [dev]
    assert fake.count(P.HELLO) == hellos + 1
    assert dev.state == D.READY and len(dev.ready_calls) == 2     # enumerated again
    assert fake.params[P.PARAM_DEBUG_EVENTS] == 1                  # events back on
    assert dev.sends_booted


def test_booted_before_start_or_after_release_is_ignored():
    fake = FakeDevice()
    dev = connect(fake)
    fake.boot()                         # not started yet
    assert dev.state == D.IDLE and fake.requests == []
    dev.start()
    dev.release()
    n = len(fake.requests)
    fake.boot()
    assert dev.state == D.IDLE and len(fake.requests) == n


def test_keepalive_slows_down_with_booted():
    """Firmware with the added GET_COMPONENT fields also sends BOOTED: poll every ~30 s, not ~5 s."""
    from fakes import C
    old = FakeDevice()
    dev = connect(old, keepalive_ticks=5, keepalive_booted_ticks=20)
    dev.start()
    assert not dev.sends_booted
    for _ in range(5):
        dev.tick()
    assert old.count(P.GET_PARAM) == 2               # enabling + one keep-alive
    new = FakeDevice([C(P.KIND_LED, 0, "", colours=1, owner=P.OWNER_HOST)])
    dev = connect(new, keepalive_ticks=5, keepalive_booted_ticks=20)
    dev.start()
    assert dev.sends_booted
    for _ in range(19):
        dev.tick()
    assert new.count(P.GET_PARAM) == 1
    dev.tick()
    assert new.count(P.GET_PARAM) == 2


def test_pads_enumerated_and_old_firmware_has_none():
    """Kind 7 after kind 6; firmware without pads or meters answers BAD_PARAM for 7 and 8."""
    comps = default_components() + [P.Component(P.KIND_PAD, 0, 0, "pad1_pads", "", "clip_1_4", led=8),
                                    P.Component(P.KIND_PAD, 1, 0, "pad2_pads", "", "clip_2_4", led=P.NONE_INDEX)]
    fake = FakeDevice(comps)
    dev = connect(fake)
    dev.start()
    assert dev.ready
    pads = dev.components[P.KIND_PAD]
    assert [(c.index, c.role, c.led) for c in pads] == [(0, "clip_1_4", 8), (1, "clip_2_4", P.NONE_INDEX)]
    assert dev.components[P.KIND_METER] == []
    old = FakeDevice()                                        # no pads: BAD_PARAM for kinds 7 and 8
    dev = connect(old)
    dev.start()
    assert dev.ready and dev.components[P.KIND_PAD] == [] and dev.components[P.KIND_METER] == []
    asked = [f.payload[0] for f in old.requests if f.cmd == P.GET_COMPONENT]
    assert P.KIND_PAD in asked and P.KIND_METER in asked


# ---- encoder modes: layer twins are live while EVENT type 8 says their mode is on ----

def _layer_components():
    E = P.KIND_ENCODER
    return [P.Component(E, 0, 0, "e1", "", "", ring=0, center=0, mode=0, layer=0),
            P.Component(E, 1, 0, "e1_zyn", "", "zynthian.zynpot_1", ring=0, center=0, mode=0, layer=4),
            P.Component(E, 2, 0, "e1_dev", "", "device_param_1", ring=0, center=0, mode=0, layer=5)]


def test_layer_replay_after_enabling_events():
    for first in (False, True):             # the replay may come before or after the SET_PARAM reply
        fake = FakeDevice(_layer_components())
        fake.modes_on = {0, 3}
        fake.replay_first = first
        dev = connect(fake)
        dev.start()
        assert dev.ready and dev.layers_on == {0, 3}, first
        e = dev.components[P.KIND_ENCODER]
        assert [dev.is_live(c) for c in e] == [True, True, False]
        assert [ev.type for ev in dev.events] == ([] if first else [P.EV_LAYER, P.EV_LAYER])


def test_layer_events_switch_twins():
    fake = FakeDevice(_layer_components())
    dev = connect(fake)
    dev.start()
    e = dev.components[P.KIND_ENCODER]
    assert dev.layers_on == set() and [dev.is_live(c) for c in e] == [True, False, False]
    fake.set_mode(3, False)
    fake.set_mode(4, True)
    assert dev.layers_on == {4} and [dev.is_live(c) for c in e] == [True, False, True]
    assert [(ev.type, ev.index, ev.value) for ev in dev.events] == [(8, 3, 0), (8, 4, 1)]
    fake.boot()                             # a restart: re-synced from the replay
    assert dev.ready and dev.layers_on == {4}
    fake.modes_on = set()                   # power-on starts in another mode, nothing on here
    fake.boot()
    assert dev.ready and dev.layers_on == set()


def test_old_firmware_everything_live():
    """No `layer` byte (and no EVENT 8): every component counts as live."""
    fake = FakeDevice()
    dev = connect(fake)
    dev.start()
    assert all(c.layer is None and dev.is_live(c) for c in dev.all_components())


def test_layer_events_before_hello_ignored():
    fake = FakeDevice(_layer_components())
    dev = connect(fake)
    fake.out(P.build_frame(P.EVENT, 0, P.build_event(P.EV_LAYER, 4, 1)))
    assert dev.layers_on == set()
