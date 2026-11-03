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
    assert (n("channel_left"), n("channel_right")) == ("track_prev", "track_next")
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
