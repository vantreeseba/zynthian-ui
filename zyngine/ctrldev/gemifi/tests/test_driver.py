# -*- coding: utf-8 -*-
"""The Zynthian driver against a fake gemifi device and a fake Zynthian (state manager,
chains, mixer, zynseq). The real driver module is loaded; the Zynthian modules it imports
are stubbed below."""

import sys
import types
import importlib
from collections import OrderedDict

import pytest

from gemifi import protocol as P
from fakes import C, FakeDevice

# ---- stub the Zynthian modules the driver imports ----

SENT = {}           # idev_out -> callable(bytes)


def _dev_send_midi_event(idev, msg, n):
    assert isinstance(msg, bytes) and len(msg) == n
    SENT[idev](msg)


class _BaseStub(object):
    """The parts of zynthian_ctrldev_zynmixer the driver relies on."""

    def __init__(self, state_manager, idev_in, idev_out=None):
        self.state_manager = state_manager
        self.chain_manager = state_manager.chain_manager
        self.zynseq = state_manager.zynseq
        self.idev = idev_in
        self.idev_out = idev_out
        self.chain_ids_filtered = []
        self.scroll_h = 0
        self.cols = 0

    def init(self):
        self.refresh()

    def end(self):
        self.light_off()

    def refresh(self):
        self.chain_ids_filtered = list(self.chain_manager.chains)

    def on_active_chain(self, active_chain_id):
        pass


def _load_driver():
    zyncore = types.ModuleType("zyncoder.zyncore")
    zyncore.lib_zyncore = types.SimpleNamespace(dev_send_midi_event=_dev_send_midi_event)
    sys.modules.setdefault("zyncoder", types.ModuleType("zyncoder"))
    sys.modules["zyncoder.zyncore"] = zyncore
    base = types.ModuleType("zyngine.ctrldev.zynthian_ctrldev_base")
    base.zynthian_ctrldev_zynmixer = _BaseStub
    sys.modules["zyngine.ctrldev.zynthian_ctrldev_base"] = base
    mod = importlib.import_module("zyngine.ctrldev.zynthian_ctrldev_gemifi")
    mod.TICK_SECONDS = 3600         # the tests tick by hand
    return mod


DRIVER = _load_driver()

# ---- a fake Zynthian ----


class FakeZctrl(object):
    def __init__(self, value, lo=0, hi=1, nudge_factor=0.01):
        self.value, self.value_min, self.value_max = value, lo, hi
        self.nudge_factor = nudge_factor
        self.readonly = False

    def set_value(self, v):
        self.value = min(self.value_max, max(self.value_min, v))

    def nudge(self, d):
        self.set_value(self.value + d * self.nudge_factor)


class FakeChain(object):
    def __init__(self, chain_id):
        self.chain_id = chain_id
        self.zynmixer_proc = types.SimpleNamespace(controllers_dict={
            "level": FakeZctrl(0.8), "balance": FakeZctrl(0.0, -1, 1),
            "mute": FakeZctrl(0, 0, 1, 1), "solo": FakeZctrl(0, 0, 1, 1), "record": FakeZctrl(0, 0, 1, 1)})

    def z(self, symbol):
        return self.zynmixer_proc.controllers_dict[symbol]


class FakeChainManager(object):
    def __init__(self, n):
        self.chains = OrderedDict((i, FakeChain(i)) for i in range(1, n + 1))
        self.chains[0] = FakeChain(0)       # main chain, shown last
        self.active_chain = self.chains[1]

    def get_active_chain(self):
        return self.active_chain

    def set_active_chain_by_id(self, chain_id):
        self.active_chain = self.chains[chain_id]


class FakeZynseq(object):
    def __init__(self):
        self.tempo = 120.0
        self.zctrl_metro_mode = FakeZctrl(0, 0, 5, 1)
        self.scene, self.phrase, self.phrases = 0, 0, 4
        self.toggled = []
        self.libseq = types.SimpleNamespace(togglePlayState=lambda s, p, c: self.toggled.append((s, p, c)))

    def get_tempo(self):
        return self.tempo

    def set_tempo(self, t):
        self.tempo = t

    def select_phrase(self, p):
        self.phrase = p


class FakeStateManager(object):
    def __init__(self, n_chains=10):
        self.chain_manager = FakeChainManager(n_chains)
        self.zynseq = FakeZynseq()
        self.zynmixer_chan = self.zynmixer_bus = None
        self.status_audio_player = False
        self.status_midi_player = self.status_midi_recorder = False
        self.audio_recorder = types.SimpleNamespace(rec_proc=None)
        self.session_record_mode = False
        self.cuias = []

    def send_cuia(self, name, params=None):
        self.cuias.append((name, params))


def zyn_components():
    B, A, E, L = P.KIND_BUTTON, P.KIND_ANALOG, P.KIND_ENCODER, P.KIND_LED
    return [
        C(B, 0, "play", led=0),
        C(B, 1, "stop", led=1),
        C(B, 2, "record", led=2),
        C(B, 3, "mute_1", led=3),
        C(B, 4, "solo_1", led=4),
        C(B, 5, "select_2", led=5),
        C(B, 6, "bank_right", led=6),       # mcu: name -> bank_next
        C(B, 7, "loop"),                    # nothing in Zynthian: logged, ignored
        C(B, 8, "metronome", led=7),
        C(B, 9, "scene_next"),
        C(B, 10, "scene_launch"),
        C(B, 11, "master_mute", led=8),
        C(A, 0, "volume_1", takeover=P.TAKEOVER_PICKUP),
        C(A, 1, "fader_2", takeover=P.TAKEOVER_NONE),
        C(A, 2, "master"),                  # mcu: name -> master_volume
        C(A, 3, "device_param_2", takeover=P.TAKEOVER_NONE),
        C(E, 0, "vpot_1", ring=11, mode=P.ENC_REL_TWOS),     # -> pan_1
        C(E, 1, "device_param_1", ring=11, mode=P.ENC_REL_TWOS),
        C(E, 2, "tempo", ring=0, mode=P.ENC_REL_TWOS),
        C(E, 3, "device_param_5", ring=0, mode=P.ENC_REL_TWOS),  # only 4 zynpots
    ] + [C(L, i, "", colours=1, owner=P.OWNER_HOST_THEN_LOCAL) for i in range(9)]


class Rig(object):
    def __init__(self, components=None, caps=None, n_chains=10, logs=None, fader_pickup=None):
        kw = {} if caps is None else {"caps": caps}
        self.dev = FakeDevice(zyn_components() if components is None else components, **kw)
        self.sm = FakeStateManager(n_chains)
        self.drv = DRIVER.zynthian_ctrldev_gemifi(self.sm, 3, 5)
        if fader_pickup is not None:
            self.drv.fader_pickup = fader_pickup
        SENT[5] = self.dev.wire
        self.dev.out = self.drv.midi_event

    def chain(self, cid):
        return self.sm.chain_manager.chains[cid]

    def event(self, etype, index, value):
        self.dev.event(etype, index, value)

    def leds(self):
        """{led: level} from the FB_LED / FB_LEDS notifications so far (popped)."""
        out = {}
        for cmd, pl in self.dev.take():
            if cmd == P.FB_LED:
                out[P.dec_u14(pl[0:2])] = pl[2]
            elif cmd == P.FB_LEDS:
                first, n = P.dec_u14(pl[0:2]), pl[2]
                for i in range(n):
                    out[first + i] = pl[3 + i]
        return out


@pytest.fixture
def rig():
    r = Rig()
    r.drv.init()
    yield r
    r.drv.end()


@pytest.fixture
def pickup_rig():
    r = Rig(fader_pickup=True)
    r.drv.init()
    yield r
    r.drv.end()


def _values(notes, kind):
    return [(P.dec_u14(pl[1:3]), P.dec_u14(pl[3:5])) for cmd, pl in notes if cmd == P.FB_VALUE and pl[0] == kind]


# ---- tests ----

def test_class_contract():
    cls = DRIVER.zynthian_ctrldev_gemifi
    assert cls.__name__ == "zynthian_ctrldev_gemifi"
    assert cls.dev_ids == ["*"]
    assert cls.unroute_from_chains is False
    assert cls.fader_pickup is False


def test_connects_enables_events_and_sends_initial_state(pickup_rig):
    rig = pickup_rig
    assert rig.drv.device.ready
    assert rig.dev.params[P.PARAM_DEBUG_EVENTS] == 1
    notes = rig.dev.take()
    assert (P.FB_RELEASE, P.fb_release(P.RELEASE_VALUES)) not in notes
    leds = {}
    for cmd, pl in notes:
        if cmd == P.FB_LED:
            leds[P.dec_u14(pl[0:2])] = pl[2]
        elif cmd == P.FB_LEDS:
            for i in range(pl[2]):
                leds[P.dec_u14(pl[0:2]) + i] = pl[3 + i]
    assert leds[0] == 0 and leds[6] == 127      # stopped; 10 chains, so bank_next is possible
    # pick-up target only for the takeover analog (volume_1) and the master fader
    assert _values(notes, P.KIND_ANALOG) == [(0, round(0.8 * P.U14_MAX)), (2, round(0.8 * P.U14_MAX))]
    assert not _values(notes, P.KIND_ENCODER)   # relative encoders take no FB_VALUE


def test_notes_pass_through_and_gemifi_sysex_is_claimed(rig):
    assert rig.drv.midi_event(bytes([0x90, 60, 100])) is False
    assert rig.drv.midi_event(bytes([0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7])) is False
    assert rig.drv.midi_event(P.build_frame(P.EVENT, 0, P.build_event(1, 99, 1))) is True


def test_unsupported_roles_logged_once(rig, caplog):
    roles = [r for _k, _i, r in rig.drv.mapping.unknown_roles]
    assert "loop" in roles and "device_param_5" in roles
    assert "play" not in roles and "volume_1" not in roles


def test_fader_sets_chain_level_without_echo(pickup_rig):
    rig = pickup_rig
    rig.dev.take()
    rig.event(P.KIND_ANALOG, 0, 0x2000)                 # far from 0.8: not picked up yet
    assert rig.chain(1).z("level").value == 0.8
    rig.event(P.KIND_ANALOG, 0, 13100)                  # reaches it
    rig.event(P.KIND_ANALOG, 0, 0x2000)
    assert rig.chain(1).z("level").value == pytest.approx(0x2000 / P.U14_MAX)
    rig.drv.tick()
    assert not _values(rig.dev.take(), P.KIND_ANALOG)


def test_level_changed_on_screen_moves_pickup_target(pickup_rig):
    rig = pickup_rig
    rig.dev.take()
    rig.chain(1).z("level").set_value(0.25)
    rig.chain(2).z("level").set_value(0.25)
    rig.drv.update_mixer_strip(0, "level", 0.25)
    vals = _values(rig.dev.take(), P.KIND_ANALOG)
    assert vals == [(0, round(0.25 * P.U14_MAX))]       # fader_2 has no pick-up: nothing


def test_jump_mode_releases_targets_and_sends_none(rig):
    notes = rig.dev.take()
    assert (P.FB_RELEASE, P.fb_release(P.RELEASE_VALUES)) in notes
    assert not _values(notes, P.KIND_ANALOG)
    rig.chain(1).z("level").set_value(0.25)
    rig.drv.update_mixer_strip(0, "level", 0.25)
    assert not _values(rig.dev.take(), P.KIND_ANALOG)


def test_jump_mode_fader_jumps_at_once(rig):
    rig.dev.take()
    rig.event(P.KIND_ANALOG, 0, 0x2000)                 # far from 0.8: jumps anyway
    assert rig.chain(1).z("level").value == pytest.approx(0x2000 / P.U14_MAX)
    rig.event(P.KIND_ANALOG, 2, 0)                      # master
    assert rig.drv.song.master_param("volume").get_norm() == 0.0
    rig.drv.tick()
    assert not _values(rig.dev.take(), P.KIND_ANALOG)


def test_mute_button_toggles_and_led_follows_screen(rig):
    rig.dev.take()
    rig.event(P.KIND_BUTTON, 3, 1)
    assert rig.chain(1).z("mute").value == 1
    assert rig.leds()[3] == 127
    rig.chain(1).z("mute").set_value(0)                 # unmuted on the touchscreen
    rig.drv.tick()
    assert rig.leds() == {3: 0}


def test_master_mute_toggles_main_chain(rig):
    rig.leds()
    rig.event(P.KIND_BUTTON, 11, 1)
    rig.event(P.KIND_BUTTON, 11, 0)
    assert rig.chain(0).z("mute").value == 1
    assert rig.chain(1).z("mute").value == 0
    rig.drv.tick()
    assert rig.leds().get(8) == 127
    rig.chain(0).z("mute").set_value(0)                 # unmuted on the touchscreen
    rig.drv.tick()
    assert rig.leds().get(8) == 0


def test_play_record_stop_use_audio_cuias(rig):
    rig.event(P.KIND_BUTTON, 0, 1)
    rig.event(P.KIND_BUTTON, 2, 1)
    rig.event(P.KIND_BUTTON, 1, 1)
    assert [c for c, _p in rig.sm.cuias] == ["TOGGLE_AUDIO_PLAY", "TOGGLE_AUDIO_RECORD",
                                             "STOP_AUDIO_PLAY", "STOP_AUDIO_RECORD"]
    rig.dev.take()
    rig.sm.status_audio_player = True
    rig.sm.audio_recorder.rec_proc = object()
    rig.drv.tick()
    leds = rig.leds()
    assert leds[0] == 127 and leds[2] == 127            # recording shows solid, not blinking


def test_bank_next_moves_strips(rig):
    rig.event(P.KIND_BUTTON, 6, 1)
    assert rig.drv.mapping.bank_offset == 8
    rig.drv.tick()
    assert rig.drv.scroll_h == 8
    rig.event(P.KIND_BUTTON, 3, 1)                      # mute_1 is chain 9 now
    assert rig.chain(9).z("mute").value == 1 and rig.chain(1).z("mute").value == 0


def test_select_sets_active_chain(rig):
    rig.event(P.KIND_BUTTON, 5, 1)
    assert rig.sm.chain_manager.active_chain.chain_id == 2
    assert rig.leds()[5] == 127


def test_pan_encoder_nudges_balance_with_boost_cut_ring(rig):
    rig.dev.take()
    rig.event(P.KIND_ENCODER, 0, P.int_to_s14(3))
    assert rig.chain(1).z("balance").value == pytest.approx(0.03)
    rings = [pl for cmd, pl in rig.dev.take() if cmd == P.FB_RING]
    assert rings and rings[-1][2] == P.RING_BOOST_CUT and rings[-1][5] & 1


def test_device_params_drive_zynpots(rig):
    notes = rig.dev.take()
    rig.event(P.KIND_ENCODER, 1, P.int_to_s14(-2))
    rig.event(P.KIND_ANALOG, 3, P.U14_MAX)
    assert rig.sm.cuias == [("ZYNPOT", [0, -2]), ("ZYNPOT_ABS", [1, 1.0])]
    rings = [pl for cmd, pl in notes if cmd == P.FB_RING and P.dec_u14(pl[0:2]) == 1]
    assert rings and rings[0][5] & 2                    # ring off: Zynthian owns the value


def test_tempo_and_metronome(rig):
    rig.event(P.KIND_ENCODER, 2, P.int_to_s14(5))
    assert rig.sm.zynseq.tempo == 125
    rig.event(P.KIND_BUTTON, 8, 1)
    assert rig.sm.zynseq.zctrl_metro_mode.value == 1
    rig.event(P.KIND_BUTTON, 8, 1)
    assert rig.sm.zynseq.zctrl_metro_mode.value == 0


def test_scenes_are_zynseq_phrases(rig):
    rig.event(P.KIND_BUTTON, 9, 1)
    rig.event(P.KIND_BUTTON, 10, 1)
    assert rig.sm.zynseq.phrase == 1
    assert rig.sm.zynseq.toggled == [(0, 1, 32)]


def test_booted_reenumerates_and_resends(rig):
    rig.dev.take()
    hellos = rig.dev.count(P.GET_COMPONENT)
    rig.dev.boot()
    assert rig.dev.count(P.GET_COMPONENT) > hellos
    assert rig.drv.device.ready and rig.dev.params[P.PARAM_DEBUG_EVENTS] == 1
    assert rig.leds()                                   # everything sent again


def test_sleep_turns_leds_off_and_wake_restores(rig):
    rig.event(P.KIND_BUTTON, 3, 1)                      # mute_1 on
    rig.dev.take()
    rig.drv.sleep_on()
    assert set(rig.leds().values()) == {0}
    rig.chain(2).z("solo").set_value(1)
    rig.drv.tick()
    assert not rig.dev.take()                           # quiet while asleep
    rig.drv.sleep_off()
    assert rig.leds()[3] == 127


def test_end_releases_and_restores_debug_events():
    r = Rig()
    r.drv.init()
    r.dev.take()
    r.drv.end()
    assert (P.FB_RELEASE, bytes([0])) in r.dev.take()
    assert r.dev.params[P.PARAM_DEBUG_EVENTS] == 0


def test_device_without_feedback_gets_nothing_but_hello():
    r = Rig(caps=P.CAP_CONFIG | P.CAP_EVENT)
    r.drv.init()
    r.drv.tick()
    r.drv.end()
    assert [f.cmd for f in r.dev.requests] == [P.HELLO]
    assert not r.dev.notifications


def test_no_output_port_sends_nothing():
    r = Rig()
    r.drv.idev_out = None
    r.drv.init()
    assert not r.dev.requests
    r.drv.end()
