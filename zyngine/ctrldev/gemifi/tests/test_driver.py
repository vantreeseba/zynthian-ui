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
    """The parts of zynthian_ctrldev_base the driver relies on."""

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


class _ZynmixerStub(_BaseStub):
    pass


class _ZynpadStub(_BaseStub):
    """zynthian_ctrldev_zynpad: records pad presses (its toggle logic is Zynthian's own)."""

    def __init__(self, state_manager, idev_in, idev_out=None):
        super().__init__(state_manager, idev_in, idev_out)
        self.pad_calls = []

    def on_pad_press(self, phrase, midi_chan):
        self.pad_calls.append(("press", phrase, midi_chan))

    def on_pad_release(self, phrase, midi_chan):
        self.pad_calls.append(("release", phrase, midi_chan))

    def refresh(self):
        raise AssertionError("the driver must not use zynpad's refresh (light_off + update_pad)")


# Launcher colours as zynthian_gui_config has them: groups 0..4 red, green, blue, yellow,
# magenta, the rest teal; the state colours are Zynthian's.
LAUNCHER_HEX = ["#FF0000", "#00FF00", "#0000FF", "#FFFF00", "#FF00FF"] + ["#3F94A2"] * 28
GUI_CONFIG = types.SimpleNamespace(
    zynswitch_bold_us=300 * 1000, zynswitch_long_us=2000 * 1000,
    LAUNCHER_COLOUR=[{"rgb": h} for h in LAUNCHER_HEX],
    LAUNCHER_STARTING_COLOUR={"rgb": "#009000"}, LAUNCHER_STOPPING_COLOUR={"rgb": "#D00000"},
    LAUNCHER_REC_COLOUR={"rgb": "#FF2020"})


def _load_driver():
    zyncore = types.ModuleType("zyncoder.zyncore")
    zyncore.lib_zyncore = types.SimpleNamespace(dev_send_midi_event=_dev_send_midi_event)
    sys.modules.setdefault("zyncoder", types.ModuleType("zyncoder"))
    sys.modules["zyncoder.zyncore"] = zyncore
    base = types.ModuleType("zyngine.ctrldev.zynthian_ctrldev_base")
    base.zynthian_ctrldev_zynmixer = _ZynmixerStub
    base.zynthian_ctrldev_zynpad = _ZynpadStub
    sys.modules["zyngine.ctrldev.zynthian_ctrldev_base"] = base
    zyngui = sys.modules.setdefault("zyngui", types.ModuleType("zyngui"))
    zyngui.zynthian_gui_config = GUI_CONFIG
    mod = importlib.import_module("zyngine.ctrldev.zynthian_ctrldev_gemifi")
    mod.TICK_SECONDS = 3600         # the tests tick by hand
    # ... and flush by hand: the session thread only waits for end()
    mod.real_run = mod.zynthian_ctrldev_gemifi._run
    mod.zynthian_ctrldev_gemifi._run = lambda self: self._stop.wait()
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
        self.midi_chan = None if chain_id == 0 else chain_id - 1
        self.clippy = None
        self.current_processor = None
        self.zynmixer_proc = types.SimpleNamespace(controllers_dict={
            "level": FakeZctrl(0.8), "balance": FakeZctrl(0.0, -1, 1),
            "mute": FakeZctrl(0, 0, 1, 1), "solo": FakeZctrl(0, 0, 1, 1), "record": FakeZctrl(0, 0, 1, 1)})

    def z(self, symbol):
        return self.zynmixer_proc.controllers_dict[symbol]

    def get_name(self):
        return "Main" if self.chain_id == 0 else "Pianoteq %d" % self.chain_id

    def get_clippy_processor(self):
        return self.clippy


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
    """zynseq's launcher state as zynseq.state holds it: scenes -> phrases -> sequences[midi chan]
    (33 per phrase in Zynthian; 32 here, the phrase launcher isn't used)."""

    def __init__(self, phrases=4):
        self.tempo = 120.0
        self.zctrl_metro_mode = FakeZctrl(0, 0, 5, 1)
        self.scene, self.phrase, self.phrases = 0, 0, phrases
        self.toggled = []
        self.libseq = types.SimpleNamespace(togglePlayState=lambda s, p, c: self.toggled.append((s, p, c)))
        self.state = {"scenes": [{"phrases": [
            {"sequences": [{"group": c, "repeat": 1, "state": 0, "nEvents": 0} for c in range(32)]}
            for _p in range(phrases)]}]}

    def seq(self, phrase, chan):
        return self.state["scenes"][self.scene]["phrases"][phrase]["sequences"][chan]

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
        C(A, 3, "zynthian.zynpot_2", takeover=P.TAKEOVER_NONE),
        C(E, 0, "vpot_1", ring=11, mode=P.ENC_REL_TWOS),     # -> pan_1
        C(E, 1, "zynthian.zynpot_1", ring=11, mode=P.ENC_REL_TWOS),
        C(E, 2, "tempo", ring=0, mode=P.ENC_REL_TWOS),
        C(E, 3, "zynthian.zynpot_5", ring=0, mode=P.ENC_REL_TWOS),  # only 4 zynpots
    ] + [C(L, i, "", colours=1, owner=P.OWNER_HOST_THEN_LOCAL) for i in range(9)] + [
        C(P.KIND_DISPLAY, 0, "", cols=6, rows=1, display_type=1)]


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

    def event(self, etype, index, value, t_ms=1234):
        self.dev.event(etype, index, value, t_ms)

    def leds(self, notes=None):
        """{led: level} from the FB_LED / FB_LEDS notifications in `notes` (default: popped so far)."""
        out = {}
        for cmd, pl in (self.dev.take() if notes is None else notes):
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
    assert "loop" in roles and "zynthian.zynpot_5" in roles
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
    rig.drv.flush()
    vals = _values(rig.dev.take(), P.KIND_ANALOG)
    assert vals == [(0, round(0.25 * P.U14_MAX))]       # fader_2 has no pick-up: nothing


def test_mixer_signals_coalesce_into_one_refresh(pickup_rig):
    rig = pickup_rig
    rig.dev.take()
    for i in range(1, 21):                              # a fader sweep on the touchscreen
        rig.chain(1).z("level").set_value(i / 20.0)
        rig.drv.update_mixer_strip(0, "level", i / 20.0)
    assert not rig.dev.take()                           # nothing until the flush
    rig.drv.flush()
    assert _values(rig.dev.take(), P.KIND_ANALOG) == [(0, P.U14_MAX)]   # one FB_VALUE, the last level
    rig.drv.flush()
    assert not rig.dev.take()                           # nothing dirty: nothing sent


def test_session_thread_flushes_soon_after_a_signal(monkeypatch):
    """The real session loop: a mixer signal is sent within a few FLUSH_SECONDS, without a tick."""
    import time
    monkeypatch.setattr(DRIVER.zynthian_ctrldev_gemifi, "_run", DRIVER.real_run)
    r = Rig(fader_pickup=True)
    r.drv.init()
    try:
        r.dev.take()
        r.chain(1).z("level").set_value(0.5)
        r.drv.update_mixer_strip(0, "level", 0.5)
        deadline = time.monotonic() + 2.0
        got = []
        while not got and time.monotonic() < deadline:
            time.sleep(0.01)
            got = _values(r.dev.take(), P.KIND_ANALOG)
        assert got == [(0, round(0.5 * P.U14_MAX))]
    finally:
        r.drv.end()
    assert r.drv._thread is None


def test_one_refresh_reads_the_chains_once(rig):
    calls = []
    real = rig.drv.song.bank_tracks
    rig.drv.song.bank_tracks = lambda: calls.append(1) or real()
    rig.drv.tick()
    assert len(calls) == 1                              # strips, bank LEDs, master: one read
    rig.event(P.KIND_BUTTON, 3, 1)                      # mute_1: the press reads, then a refresh
    assert len(calls) <= 3


def test_flush_is_quiet_while_asleep(rig):
    rig.drv.sleep_on()
    rig.dev.take()
    rig.chain(1).z("mute").set_value(1)
    rig.drv.update_mixer_strip(0, "mute", 1)
    rig.drv.flush()
    assert not rig.dev.take()


def test_jump_mode_releases_targets_and_sends_none(rig):
    notes = rig.dev.take()
    assert (P.FB_RELEASE, P.fb_release(P.RELEASE_VALUES)) in notes
    assert not _values(notes, P.KIND_ANALOG)
    rig.chain(1).z("level").set_value(0.25)
    rig.drv.update_mixer_strip(0, "level", 0.25)
    rig.drv.flush()
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


def _texts(notes):
    return [(P.dec_u14(pl[0:2]), pl[2], pl[3], pl[4:].decode("ascii")) for cmd, pl in notes if cmd == P.FB_TEXT]


def test_display_shows_active_chain_name(rig):
    # sent on connect, cut to the display's 6 columns
    assert rig.drv.mapping._text_cache == {0: "Pianot"}
    rig.dev.take()
    rig.drv.tick()
    assert not _texts(rig.dev.take())                   # change-only
    rig.sm.chain_manager.set_active_chain_by_id(0)
    rig.drv.tick()
    assert _texts(rig.dev.take()) == [(0, 0, 0, "Main  ")]


def test_display_text_is_ascii_padded():
    from gemifi.zynthian_model import _display_text
    assert _display_text("Café ⏺", 6) == "Cafe  "
    assert _display_text(None, 3) == "   "


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
    rig.event(P.KIND_ENCODER, 0, P.int_to_s14(20))
    assert rig.chain(1).z("balance").value == pytest.approx(0.2)
    rings = [pl for cmd, pl in rig.dev.take() if cmd == P.FB_RING]
    assert rings and rings[-1][2] == P.RING_BOOST_CUT and rings[-1][5] & 1


def test_ring_sent_only_when_its_leds_change(rig):
    rig.dev.take()
    rig.event(P.KIND_ENCODER, 0, P.int_to_s14(3))       # balance 0.03: the 11-LED ring still shows centre
    assert rig.chain(1).z("balance").value == pytest.approx(0.03)
    assert not [pl for cmd, pl in rig.dev.take() if cmd == P.FB_RING]
    rig.event(P.KIND_ENCODER, 0, P.int_to_s14(7))       # 0.1: one LED to the right
    rings = [pl for cmd, pl in rig.dev.take() if cmd == P.FB_RING]
    assert len(rings) == 1 and P.dec_u14(rings[0][3:5]) == round(0.55 * P.U14_MAX)


def test_ring_lit_follows_the_firmware():
    from gemifi.mapping import ring_lit
    assert [ring_lit(11, P.RING_WRAP, v) for v in (0, 818, 820, 8192, P.U14_MAX)] == [0, 0, 1, 5, 10]
    assert [ring_lit(15, P.RING_SPREAD, v) for v in (0, 2340, 2341, P.U14_MAX)] == [0, 0, 1, 7]


def test_accelerated_turn_moves_by_the_whole_delta(rig):
    rig.event(P.KIND_ENCODER, 0, P.int_to_s14(-12))     # a quick turn: 12 steps in one EVENT
    assert rig.chain(1).z("balance").value == pytest.approx(-0.12)
    rig.event(P.KIND_ENCODER, 2, P.int_to_s14(9))       # tempo: 9 BPM
    assert rig.sm.zynseq.tempo == 129


def test_zynpot_roles_drive_zynpots(rig):
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
    notes = rig.dev.take()
    assert set(rig.leds(notes).values()) == {0}
    assert _texts(notes) == [(0, 0, 0, "      ")]       # display blank too
    rig.chain(2).z("solo").set_value(1)
    rig.drv.tick()
    assert not rig.dev.take()                           # quiet while asleep
    rig.drv.sleep_off()
    notes = rig.dev.take()
    assert rig.leds(notes)[3] == 127
    assert _texts(notes) == [(0, 0, 0, "Pianot")]


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


# ---- issue #21: clip grid, nav roles, zynthian.<cuia>, the param display ----

from gemifi import zynthian_model as ZM

BLACK = (0, 0, 0, 0)
STARTING, STOPPING = (0, 72, 0, 1), (104, 0, 0, 1)         # Zynthian's #009000 / #D00000, blinking
RECORDING, REC_QUEUED = (127, 16, 16, 0), (127, 16, 16, 1)  # #FF2020
PAD_LED0 = 3                        # pad k (1..16) has LED PAD_LED0 + k - 1


def full(group):
    """A playing pad of `group`: its launcher colour."""
    return ZM.rgb7(LAUNCHER_HEX[group]) + (0,)


def idle(group):
    """A stopped pad of `group`: its launcher colour, dimmed."""
    return ZM.dim(ZM.rgb7(LAUNCHER_HEX[group]), ZM.CLIP_IDLE_SCALE) + (0,)


def code61_pad_role(k):
    """The Code 61's pad layer (contract §4): pad 1 bottom left, rows of 4, top row = scene 1."""
    return "clip_%d_%d" % ((k - 1) % 4 + 1, 4 - (k - 1) // 4)


def grid_components():
    B, A, E, L, PAD = P.KIND_BUTTON, P.KIND_ANALOG, P.KIND_ENCODER, P.KIND_LED, P.KIND_PAD
    return [
        C(B, 0, "shift"),
        C(B, 1, "grid_left", led=0),
        C(B, 2, "grid_right", led=1),
        C(B, 3, "grid_up"),
        C(B, 4, "grid_down", led=2),
        C(B, 5, "nav_back"),
        C(B, 6, "nav_select"),
        C(B, 7, "nav_home"),
        C(B, 8, "nav_alt"),
        C(B, 9, "nav_up"),
        C(B, 10, "zynthian.toggle_audio_play"),
        C(B, 11, "bitwig.launcher"),        # another host's command: ignored quietly
        C(B, 12, "zynthian."),              # no command: unknown
        C(B, 13, "strip_next"),
        C(B, 14, "strip_prev"),
        C(B, 15, "select_2"),
        C(B, 16, "meter_1"),                # reserved: unknown
        C(A, 0, "volume_1", takeover=P.TAKEOVER_NONE, touch=P.TOUCH_MOVEMENT),
        C(E, 0, "pan_1", ring=0, mode=P.ENC_REL_TWOS),
        C(E, 1, "nav_scroll", ring=0, mode=P.ENC_REL_TWOS),
        C(E, 2, "tempo", ring=0, mode=P.ENC_REL_TWOS),
        C(E, 3, "zynthian.zynpot_1", ring=0, mode=P.ENC_REL_TWOS),
    ] + [C(PAD, k - 1, code61_pad_role(k), led=PAD_LED0 + k - 1) for k in range(1, 17)] + [
        C(PAD, 16, "play", led=19),         # a pad twin with a button role
        C(PAD, 17, ""),                     # a base pad: plays notes, no role
    ] + [C(L, i, "", colours=3 if i >= PAD_LED0 else 1, owner=P.OWNER_HOST_THEN_LOCAL) for i in range(20)] + [
        C(P.KIND_DISPLAY, 0, "param", cols=6, rows=1, display_type=1),
        C(P.KIND_DISPLAY, 1, "time", cols=8, rows=1, display_type=1),
    ]


def rgbs(notes):
    return dict((P.dec_u14(pl[0:2]), (pl[2], pl[3], pl[4], pl[5])) for cmd, pl in notes if cmd == P.FB_LED_RGB)


def pad_led(t, s):
    """LED of the pad with role clip_t_s."""
    for k in range(1, 17):
        if code61_pad_role(k) == "clip_%d_%d" % (t, s):
            return PAD_LED0 + k - 1


def pad_index(t, s):
    return pad_led(t, s) - PAD_LED0


@pytest.fixture
def grid():
    r = Rig(components=grid_components())
    r.sm.zynseq = FakeZynseq(phrases=6)
    r.drv.init()
    yield r
    r.drv.end()


def test_code61_pad_roles_match_the_contract():
    assert [code61_pad_role(k) for k in (1, 4, 5, 13, 16)] == [
        "clip_1_4", "clip_4_4", "clip_1_3", "clip_1_1", "clip_4_1"]
    assert ZM.clip_role("clip_3_2") == (3, 2)
    for bad in ("clip_0_1", "clip_9_1", "clip_1", "clip_a_b", "clip_1_1_1", ""):
        assert ZM.clip_role(bad) == (None, None)


def test_grid_cells_follow_scroll(grid):
    m = grid.drv.mapping
    cb = m._clips[(P.KIND_PAD, pad_index(2, 3))]
    track, phrase = m.clip_cell(cb)
    assert (track.key, phrase) == (2, 2)                # column 2 = the 2nd chain, row 3 = phrase 2
    m.grid_col, m.grid_row = 3, 1
    track, phrase = m.clip_cell(cb)
    assert (track.key, phrase) == (5, 3)


def test_grid_leds_show_sequence_state(grid):
    zs = grid.sm.zynseq
    notes = grid.dev.take()
    assert set(rgbs(notes).values()) == {BLACK}         # nothing recorded yet: all off
    assert len(rgbs(notes)) == 16
    zs.seq(0, 0)["nEvents"] = 5                         # chain 1 (MIDI chan 0, group 0), phrase 0: stopped
    zs.seq(1, 1)["nEvents"] = 3                         # chain 2 (group 1), phrase 1: playing
    zs.seq(1, 1)["state"] = 1
    zs.seq(2, 2)["nEvents"] = 3                         # starting: green, blinking
    zs.seq(2, 2)["state"] = 2
    zs.seq(0, 3)["nEvents"] = 3                         # stopping at the next sync: red, blinking
    zs.seq(0, 3)["state"] = 5
    zs.seq(1, 2)["nEvents"] = 3                         # recording: red
    zs.seq(1, 2)["state"] = 9
    zs.seq(2, 1)["nEvents"] = 3                         # punch-in queued: red, blinking
    zs.seq(2, 1)["state"] = 10
    zs.seq(3, 1)["nEvents"] = 3                         # child playing: as playing
    zs.seq(3, 1)["state"] = 6
    zs.seq(3, 3)["nEvents"] = 3                         # disabled (repeat 0): no sequence
    zs.seq(3, 3)["repeat"] = 0
    grid.drv.tick()
    got = rgbs(grid.dev.take())
    assert got == {pad_led(1, 1): idle(0), pad_led(2, 2): full(1), pad_led(3, 3): STARTING,
                   pad_led(4, 1): STOPPING, pad_led(3, 2): RECORDING, pad_led(2, 3): REC_QUEUED,
                   pad_led(2, 4): full(1)}
    assert idle(0) == (44, 0, 0, 0) and full(1) == (0, 127, 0, 0)
    grid.drv.tick()
    assert not rgbs(grid.dev.take())                    # change-only


def test_grid_pad_recording_midi_is_red(grid):
    grid.dev.take()
    grid.sm.midi_record_pad = (1, 2)                    # phrase 1, chain 3: even while empty
    grid.drv.tick()
    assert rgbs(grid.dev.take()) == {pad_led(3, 2): RECORDING}


def test_grid_clippy_take_shows_before_the_clip_exists(grid):
    """A clippy pad has no clip file until its take is saved: arming and recording still show."""
    chain = grid.chain(4)
    clip = FakeZctrl(0)
    chain.clippy = types.SimpleNamespace(controllers_dict={"file 1": types.SimpleNamespace(get_value=lambda: clip.value)})
    seq = grid.sm.zynseq.seq(0, 3)
    seq["group"] = 19
    grid.drv.tick()
    grid.dev.take()
    steps = [(10, REC_QUEUED),              # armed, waiting for the bar to punch in
             (9, RECORDING),                # recording, still no file
             (11, REC_QUEUED)]              # punch-out queued
    for state, rgb in steps:
        seq["state"] = state
        grid.drv.update_seq_state(phrase=0, chan=3)
        grid.drv.flush()
        assert rgbs(grid.dev.take()) == {pad_led(4, 1): rgb}, state
    seq["state"] = 1                        # take saved, playing
    clip.value = "take1.wav"
    grid.drv.tick()
    assert rgbs(grid.dev.take()) == {pad_led(4, 1): full(19)}
    seq["repeat"], seq["state"] = 0, 9      # a disabled pad stays off, as on a Launchpad
    grid.drv.tick()
    assert rgbs(grid.dev.take()) == {pad_led(4, 1): BLACK}


def test_grid_empty_midi_pad_recording_is_red(grid):
    grid.dev.take()
    grid.sm.midi_record_pad = (0, 0)        # session record on an empty MIDI pad
    grid.drv.update_seq_state(phrase=0, chan=0)
    grid.drv.flush()
    assert rgbs(grid.dev.take()) == {pad_led(1, 1): RECORDING}
    grid.sm.midi_record_pad = None          # stopped with nothing captured: off again
    grid.drv.tick()
    assert rgbs(grid.dev.take()) == {pad_led(1, 1): BLACK}


def test_grid_colours_without_zynthians_table():
    sm = FakeStateManager(3)
    song = ZM.ZynSong(sm, lambda: list(sm.chain_manager.chains))     # no launcher_colour
    track = song.bank_tracks()[0]
    sm.zynseq.seq(0, 0)["nEvents"] = 1
    assert song.clip_rgb(track, 0) == ZM.dim(ZM.DEFAULT_GROUP_RGB, ZM.CLIP_IDLE_SCALE) + (False,)
    sm.zynseq.seq(0, 0)["state"] = 3
    assert song.clip_rgb(track, 0) == ZM.DEFAULT_RGB["stopping"] + (True,)
    bad = ZM.ZynSong(sm, lambda: list(sm.chain_manager.chains), launcher_colour=lambda k: "nope")
    assert bad.clip_rgb(bad.bank_tracks()[0], 0) == ZM.DEFAULT_RGB["stopping"] + (True,)


def test_colour_helpers():
    assert ZM.rgb7("#FF8000") == (127, 64, 0) and ZM.rgb7((255, 0, 2)) == (127, 0, 1)
    assert ZM.rgb7("FF8000") == (127, 64, 0)
    assert ZM.rgb7(None) is None and ZM.rgb7("#12") is None and ZM.rgb7("#GG0000") is None
    assert ZM.dim((127, 2, 0), 0.25) == (32, 1, 0)      # a lit channel stays lit


def test_clippy_pads_lit_by_their_clip_file(grid):
    chain = grid.chain(4)
    clip = FakeZctrl(0)
    chain.clippy = types.SimpleNamespace(controllers_dict={"file 1": types.SimpleNamespace(get_value=lambda: clip.value)})
    grid.sm.zynseq.seq(0, 3)["group"] = 19              # a clippy sequence
    grid.dev.take()
    clip.value = "take1.wav"
    grid.drv.tick()
    assert rgbs(grid.dev.take()) == {pad_led(4, 1): idle(19)}


def test_seq_state_signal_refreshes_on_the_next_flush(grid):
    zs = grid.sm.zynseq
    zs.seq(0, 0)["nEvents"] = 5
    grid.drv.tick()
    grid.dev.take()
    zs.seq(0, 0)["state"] = 1
    grid.drv.update_seq_state(phrase=0, chan=0)         # zynsigman SS_SEQ_PLAY_STATE, no tick
    grid.drv.flush()
    assert rgbs(grid.dev.take()) == {pad_led(1, 1): full(0)}


def test_pad_press_launches_through_zynpad(grid):
    grid.event(P.KIND_PAD, pad_index(3, 2), 9000)
    grid.event(P.KIND_PAD, pad_index(3, 2), 0)
    assert grid.drv.pad_calls == [("press", 1, 2), ("release", 1, 2)]   # phrase 1, chain 3's chan 2


def test_pad_release_goes_to_the_pad_it_pressed(grid):
    grid.event(P.KIND_PAD, pad_index(1, 1), 100)
    grid.event(P.KIND_BUTTON, 2, 1)                     # grid_right while held
    grid.event(P.KIND_PAD, pad_index(1, 1), 0)
    assert grid.drv.pad_calls == [("press", 0, 0), ("release", 0, 0)]


def test_pad_without_a_chain_does_nothing():
    r = Rig(components=grid_components(), n_chains=2)
    r.drv.init()
    r.event(P.KIND_PAD, pad_index(3, 1), 100)           # column 3: only 2 chains
    r.event(P.KIND_PAD, pad_index(3, 1), 0)
    assert r.drv.pad_calls == []
    r.drv.end()


def test_audio_chain_has_no_launcher(grid):
    grid.chain(1).midi_chan = None
    grid.event(P.KIND_PAD, pad_index(1, 1), 100)
    assert grid.drv.pad_calls == []


def test_song_toggles_without_zynpad_callbacks():
    sm = FakeStateManager(3)
    song = ZM.ZynSong(sm, lambda: list(sm.chain_manager.chains))
    track = song.bank_tracks()[1]
    assert song.clip_press(track, 2)
    assert sm.zynseq.toggled == [(0, 2, 1)]
    assert not song.clip_press(track, 9)                # no such phrase


def test_grid_scrolls_by_one_and_clamps(grid):
    m = grid.drv.mapping
    leds = grid.leds()
    assert (leds[0], leds[1], leds[2]) == (0, 127, 127)  # can go right and down, not left
    for _ in range(10):
        grid.event(P.KIND_BUTTON, 2, 1)                 # grid_right
    assert m.grid_col == 6                              # 10 chains, 4 columns
    grid.event(P.KIND_BUTTON, 1, 1)                     # grid_left
    assert m.grid_col == 5
    for _ in range(5):
        grid.event(P.KIND_BUTTON, 4, 1)                 # grid_down
    assert m.grid_row == 2                              # 6 phrases, 4 rows
    grid.event(P.KIND_BUTTON, 3, 1)                     # grid_up
    assert m.grid_row == 1
    grid.sm.zynseq.seq(1, 5)["nEvents"] = 1             # chain 6, phrase 1: now the top left pad
    grid.drv.tick()
    assert rgbs(grid.dev.take())[pad_led(1, 1)] == idle(5)
    grid.event(P.KIND_PAD, pad_index(1, 1), 50)
    assert grid.drv.pad_calls[-1] == ("press", 1, 5)


def test_grid_offset_clamped_when_chains_go(grid):
    m = grid.drv.mapping
    m.grid_col = 6
    for cid in (7, 8, 9, 10):
        del grid.sm.chain_manager.chains[cid]
    grid.drv.refresh()
    assert m.grid_col == 2


def test_pad_with_a_button_role(grid):
    grid.leds()
    grid.event(P.KIND_PAD, 16, 3000)
    grid.event(P.KIND_PAD, 16, 0)
    assert grid.sm.cuias == [("TOGGLE_AUDIO_PLAY", None)]
    grid.sm.status_audio_player = True
    grid.drv.tick()
    assert grid.leds().get(19) == 127


def test_roles_logged_or_ignored(grid):
    roles = [r for _k, _i, r in grid.drv.mapping.unknown_roles]
    assert "zynthian." in roles and "meter_1" in roles and "time" in roles
    assert "bitwig.launcher" not in roles
    assert not [r for r in roles if r.startswith("clip_") or r.startswith("nav_") or r.startswith("grid_")]
    assert "shift" not in roles and "zynthian.toggle_audio_play" not in roles and "pan_1" not in roles


@pytest.mark.parametrize("button,switch", [(5, 1), (6, 3), (7, 0), (8, 2)])
def test_nav_buttons_are_zynswitches(grid, button, switch):
    grid.event(P.KIND_BUTTON, button, 1, t_ms=1000)
    assert grid.sm.cuias == []                          # nothing until the release
    grid.event(P.KIND_BUTTON, button, 0, t_ms=1120)
    assert grid.sm.cuias == [("ZYNSWITCH", [switch, "S"])]


@pytest.mark.parametrize("held,kind", [(0, "S"), (300, "S"), (301, "B"), (2000, "B"), (2001, "L")])
def test_nav_press_type_from_t_ms(grid, held, kind):
    grid.event(P.KIND_BUTTON, 5, 1, t_ms=P.T_MS_MASK - 100)      # across the u28 wrap
    grid.event(P.KIND_BUTTON, 5, 0, t_ms=(P.T_MS_MASK - 100 + held) & P.T_MS_MASK)
    assert grid.sm.cuias == [("ZYNSWITCH", [1, kind])]


def test_nav_uses_zynthians_thresholds(grid, monkeypatch):
    monkeypatch.setattr(GUI_CONFIG, "zynswitch_bold_us", 100 * 1000)
    monkeypatch.setattr(GUI_CONFIG, "zynswitch_long_us", 500 * 1000)
    for held in (150, 600):
        grid.event(P.KIND_BUTTON, 6, 1, t_ms=0)
        grid.event(P.KIND_BUTTON, 6, 0, t_ms=held)
    assert grid.sm.cuias == [("ZYNSWITCH", [3, "B"]), ("ZYNSWITCH", [3, "L"])]


def test_nav_release_without_press_is_ignored(grid):
    grid.event(P.KIND_BUTTON, 5, 0, t_ms=500)
    assert grid.sm.cuias == []


def test_nav_arrows_repeat_while_held(grid):
    grid.event(P.KIND_BUTTON, 9, 1)
    assert grid.sm.cuias == [("ARROW_UP", None)]
    for _ in range(4):
        grid.drv.tick()
    grid.event(P.KIND_BUTTON, 9, 0)
    grid.drv.tick()
    assert [c for c, _p in grid.sm.cuias] == ["ARROW_UP"] * 3   # repeats from the 3rd tick held


def test_nav_scroll_is_the_select_zynpot(grid):
    grid.event(P.KIND_ENCODER, 1, P.int_to_s14(-2))
    grid.event(P.KIND_ENCODER, 1, P.int_to_s14(1))
    assert grid.sm.cuias == [("ZYNPOT", [3, -2]), ("ZYNPOT", [3, 1])]


def test_zynthian_cuia_role(grid):
    grid.event(P.KIND_BUTTON, 10, 1)
    grid.event(P.KIND_BUTTON, 10, 0)
    grid.event(P.KIND_BUTTON, 11, 1)                    # bitwig.launcher: nothing
    grid.event(P.KIND_BUTTON, 12, 1)                    # "zynthian.": nothing
    assert grid.sm.cuias == [("TOGGLE_AUDIO_PLAY", None)]


def test_shift_is_tracked(grid):
    m = grid.drv.mapping
    grid.event(P.KIND_BUTTON, 0, 1)
    assert m.shift and grid.sm.cuias == []
    grid.event(P.KIND_BUTTON, 0, 0)
    assert not m.shift


def test_strip_prev_next_move_the_bank_by_one(grid):
    grid.event(P.KIND_BUTTON, 13, 1)
    grid.event(P.KIND_BUTTON, 13, 1)
    assert grid.drv.mapping.bank_offset == 2
    grid.event(P.KIND_BUTTON, 14, 1)
    assert grid.drv.mapping.bank_offset == 1
    grid.event(P.KIND_ENCODER, 0, P.int_to_s14(2))      # pan_1 is chain 2 now
    assert grid.chain(2).z("balance").value == pytest.approx(0.02)


def test_param_display_shows_values_then_the_chain_name(grid):
    assert grid.drv.mapping._text_cache == {0: "Pianot"}            # the time display isn't driven
    grid.dev.take()
    grid.event(P.KIND_ANALOG, 0, P.U14_MAX // 2)                    # volume_1, jump mode
    assert not _texts(grid.dev.take())                              # the next flush sends it
    grid.drv.flush()
    assert _texts(grid.dev.take()) == [(0, 0, 0, "V1 50%")]
    grid.event(P.KIND_ENCODER, 0, P.int_to_s14(-20))                # pan_1
    grid.drv.flush()
    assert _texts(grid.dev.take()) == [(0, 0, 0, "P1 L20")]
    grid.event(P.KIND_ENCODER, 0, P.int_to_s14(-20))
    grid.drv.flush()
    assert _texts(grid.dev.take()) == [(0, 0, 0, "P1 L40")]
    grid.chain(1).z("balance").set_value(-1.0)
    grid.event(P.KIND_ENCODER, 0, P.int_to_s14(-1))
    grid.drv.flush()
    assert _texts(grid.dev.take()) == [(0, 0, 0, "L100  ")]         # "P1 L100" doesn't fit
    grid.event(P.KIND_ENCODER, 2, P.int_to_s14(5))                  # tempo
    grid.drv.flush()
    assert _texts(grid.dev.take()) == [(0, 0, 0, "125bpm")]
    for _ in range(ZM.TEXT_HOLD_TICKS - 1):
        grid.drv.tick()
    assert not _texts(grid.dev.take())                              # still held
    grid.drv.tick()
    assert _texts(grid.dev.take()) == [(0, 0, 0, "Pianot")]         # ~1.5 s later


def test_param_display_sends_the_latest_text_once_per_flush(grid):
    grid.dev.take()
    for v in range(0, P.U14_MAX, P.U14_MAX // 40):                  # a fast fader sweep: 41 EVENTs
        grid.event(P.KIND_ANALOG, 0, v)
    grid.drv.flush()
    assert _texts(grid.dev.take()) == [(0, 0, 0, "100%  ")]         # one FB_TEXT, the last value


def test_param_display_zynpots_show_nothing(grid):
    grid.dev.take()
    grid.event(P.KIND_ENCODER, 3, P.int_to_s14(1))                  # zynthian.zynpot_1: Zynthian shows it
    grid.event(P.KIND_ENCODER, 1, P.int_to_s14(1))                  # nav_scroll
    grid.drv.flush()
    assert not _texts(grid.dev.take())


def test_param_display_selection_and_bank_show_the_name(grid):
    grid.dev.take()
    grid.event(P.KIND_ANALOG, 0, 0)
    grid.drv.flush()
    assert _texts(grid.dev.take()) == [(0, 0, 0, "V1 0% ")]
    grid.event(P.KIND_BUTTON, 15, 1)                                # select_2: name at once
    assert _texts(grid.dev.take()) == [(0, 0, 0, "Pianot")]
    grid.sm.chain_manager.chains[2].get_name = lambda: "EP"
    grid.event(P.KIND_ANALOG, 0, 0)
    grid.drv.flush()
    grid.dev.take()
    grid.event(P.KIND_BUTTON, 13, 1)                                # strip_next: bank changed
    assert _texts(grid.dev.take()) == [(0, 0, 0, "EP    ")]


def test_display_text_clipped_to_cols():
    assert ZM._display_text("Grand Piano", 6) == "Grand "
    assert ZM._display_text("Grand Piano", 16) == "Grand Piano     "
    assert ZM._display_text("125bpm", 3) == "125"


def test_touch_events_ignored(grid):
    grid.dev.take()
    grid.event(P.EV_TOUCH, 0, 1)
    grid.event(P.EV_TOUCH, 0, 0)
    assert not grid.dev.take() and grid.sm.cuias == []


def test_sleep_turns_pads_off_and_wake_restores(grid):
    grid.sm.zynseq.seq(0, 0)["nEvents"] = 1
    grid.drv.tick()
    grid.dev.take()
    grid.drv.sleep_on()
    assert pad_led(1, 1) in grid.leds(grid.dev.take())              # FB_LED 0 for every LED
    grid.drv.tick()
    assert not grid.dev.take()
    grid.drv.sleep_off()
    assert rgbs(grid.dev.take())[pad_led(1, 1)] == idle(0)


def test_driver_reuses_the_zynpad_base():
    assert issubclass(DRIVER.zynthian_ctrldev_gemifi, _ZynpadStub)
    assert issubclass(DRIVER.zynthian_ctrldev_gemifi, _ZynmixerStub)


def test_param_labels():
    assert [ZM.param_label(r) for r in ("volume_3", "pan_8", "master_volume", "track_volume", "track_pan",
                                         "tempo", "device_param_1")] == ["V3", "P8", "M", "V", "P", "", ""]


# ---- encoder modes (contract §5): zynthian.zynpot_N, device_param_N = the processor's params ----

class FakeProcZctrl(FakeZctrl):
    """A processor controller: zynthian_controller's attributes the driver reads."""

    def __init__(self, symbol, value=0, lo=0, hi=127, nudge_factor=1, short_name=None, is_integer=True,
                 ticks=None, labels=None, is_path=False, readonly=False):
        FakeZctrl.__init__(self, value, lo, hi, nudge_factor)
        self.symbol = symbol
        self.name = symbol.title()
        self.short_name = short_name or self.name
        self.is_integer, self.ticks, self.labels, self.is_path = is_integer, ticks, labels, is_path
        self.readonly = readonly
        self.nudges = []

    def set_value(self, v, send=True):
        if self.readonly:
            return
        v = min(self.value_max, max(self.value_min, v))
        self.value = int(v) if self.is_integer else v

    def nudge(self, d, send=True, fine=False):
        self.nudges.append(d)
        if self.ticks:
            i = min(range(len(self.ticks)), key=lambda k: abs(self.ticks[k] - self.value))
            self.set_value(self.ticks[min(len(self.ticks) - 1, max(0, i + d))])
        else:
            self.set_value(self.value + d * self.nudge_factor)

    def get_value2label(self, val=None):
        val = self.value if val is None else val
        return self.labels[self.ticks.index(val)]


class FakeProcessor(object):
    """zynthian_processor's control screens: an ordered dict, screen name -> list of zctrls."""

    def __init__(self, pid, name, screens):
        self.id, self.name = pid, name
        self.screens = OrderedDict(screens)

    def get_ctrl_screens(self):
        return self.screens

    def get_name(self):
        return self.name


def z(symbol, **kw):
    return FakeProcZctrl(symbol, **kw)


def synth():
    """12 parameter slots: Main (4), Filter (2, padded to 4), Env (4)."""
    return FakeProcessor(7, "OB-Xd", [
        ("Main", [z("volume", value=100), z("tune", value=64), z("osc", value=0, ticks=[0, 1, 2],
                                                                   labels=["Saw", "Pulse", "Tri"]),
                  z("glide", value=0.5, lo=0, hi=1, nudge_factor=0.01, is_integer=False)]),
        ("Filter", [z("cutoff", value=64, short_name="Cutoff"), z("reso", value=10)]),
        ("Empty", []),                              # the control screen skips it too
        ("Env", [z("attack"), z("decay"), z("sustain"), z("release")]),
    ])


# Mode indices: enc_midi 2, enc_zyn 3, enc_device 4 (arm 0 and pads 1 come first); twins carry
# layer = mode index + 1.
ENC_MIDI, ENC_ZYN, ENC_DEV = 2, 3, 4
DP_ZYN = 12             # encoder index of the enc_zyn twin of encoder_5 (device_param_1)
DP_DEV = 16             # encoder index of the enc_device twin of encoder_1 (device_param_1)
BANK_ZYN, BANK_DEV = 4, 6       # device_bank_prev button twins (device_bank_next = + 1)


def code61_enc_components(layers=True):
    """The Code 61's encoder cycle: 8 plain encoders, then the enc_zyn twins (zynpots on E1-E4,
    device_param_1..4 on E5-E8) and the enc_device twins (device_param_1..8); bank left / right
    and their twins. `layers` False: firmware from before the `layer` byte."""
    B, E = P.KIND_BUTTON, P.KIND_ENCODER
    lay = (lambda n: n) if layers else (lambda n: None)
    enc = lambda role, layer: C(E, 0, role, ring=0, mode=P.ENC_ABSOLUTE, layer=lay(layer))
    return ([C(B, 0, "shift", layer=lay(0)), C(B, 1, "nav_back", layer=lay(0)),
             C(B, 2, "bank_left", led=0, layer=lay(0)), C(B, 3, "bank_right", led=1, layer=lay(0)),
             C(B, 4, "device_bank_prev", led=2, layer=lay(ENC_ZYN + 1)),
             C(B, 5, "device_bank_next", led=3, layer=lay(ENC_ZYN + 1)),
             C(B, 6, "device_bank_prev", led=4, layer=lay(ENC_DEV + 1)),
             C(B, 7, "device_bank_next", led=5, layer=lay(ENC_DEV + 1))] +
            [enc("", 0) for _ in range(8)] +
            [enc("zynthian.zynpot_%d" % n, ENC_ZYN + 1) for n in range(1, 5)] +
            [enc("device_param_%d" % n, ENC_ZYN + 1) for n in range(1, 5)] +
            [enc("device_param_%d" % n, ENC_DEV + 1) for n in range(1, 9)] +
            [C(P.KIND_LED, i, "", colours=1, owner=P.OWNER_HOST_THEN_LOCAL, layer=lay(0)) for i in range(6)] +
            [C(P.KIND_DISPLAY, 0, "param", cols=6, rows=1, display_type=1)])


def enc_rig(modes=(ENC_ZYN,), layers=True, proc=True):
    r = Rig(components=code61_enc_components(layers))
    r.dev.modes_on = set(modes) if layers else set()
    r.dev.replay_layers = layers
    if proc:
        r.chain(1).current_processor = synth()
    r.drv.init()
    return r


@pytest.fixture
def enc():
    r = enc_rig()
    yield r
    r.drv.end()


def turn(rig, index, delta):
    rig.event(P.KIND_ENCODER, index, P.int_to_s14(delta))


def press(rig, button):
    rig.event(P.KIND_BUTTON, button, 1)
    rig.event(P.KIND_BUTTON, button, 0)


def zc(rig, symbol, chain=1):
    for zs in rig.chain(chain).current_processor.screens.values():
        for zz in zs:
            if zz.symbol == symbol:
                return zz


def test_role_helpers():
    assert [ZM.zynpot_role(r) for r in ("zynthian.zynpot_1", "zynthian.zynpot_4", "zynthian.zynpot_5",
                                        "zynthian.zynpot_0", "zynpot_1", "device_param_1")] == [0, 3, None, None,
                                                                                               None, None]
    assert [ZM.device_param_number(r) for r in ("device_param_1", "device_param_8", "device_param_9",
                                                "device_param_x", "zynthian.zynpot_1")] == [1, 8, None, None, None]


def test_zynpot_twins_drive_the_zynpots(enc):
    turn(enc, 8, 3)                             # E1 in enc_zyn: zynthian.zynpot_1
    turn(enc, 11, -1)                           # E4: zynthian.zynpot_4 -> zynpot 3
    assert enc.sm.cuias == [("ZYNPOT", [0, 3]), ("ZYNPOT", [3, -1])]
    roles = [r for _k, _i, r in enc.drv.mapping.unknown_roles]
    assert not [r for r in roles if "zynpot" in r or r.startswith("device_")]


def test_device_param_turns_the_processors_controller(enc):
    turn(enc, DP_ZYN, 2)                        # device_param_1: Main / volume
    turn(enc, DP_ZYN + 1, -4)                   # device_param_2: tune
    assert (zc(enc, "volume").value, zc(enc, "tune").value) == (102, 60)
    assert zc(enc, "volume").nudges == [2]      # the controller's own nudge
    turn(enc, DP_ZYN + 2, 1)                    # osc: one tick
    assert zc(enc, "osc").value == 1
    assert enc.sm.cuias == []                   # not the zynpots any more


def test_device_param_paging_follows_screens_and_clamps(enc):
    m = enc.drv.mapping
    assert len(enc.drv.song.device_params()) == 12 and m._device_bank_count() == 6
    assert m.device_page_step() == 2            # enc_zyn: device_param_1..4 live, 2 pages of 2
    press(enc, BANK_ZYN + 1)                    # device_bank_next
    assert m.device_bank == 2
    turn(enc, DP_ZYN, 1)                        # Filter / cutoff
    turn(enc, DP_ZYN + 2, 1)                    # a padded gap: nothing
    assert zc(enc, "cutoff").value == 65
    press(enc, BANK_ZYN + 1)
    press(enc, BANK_ZYN + 1)                    # clamped at the last page
    assert m.device_bank == 4
    turn(enc, DP_ZYN + 3, 5)
    assert zc(enc, "release").value == 5
    press(enc, BANK_ZYN)                        # device_bank_prev
    assert m.device_bank == 2
    press(enc, BANK_ZYN)
    press(enc, BANK_ZYN)
    assert m.device_bank == 0


def test_device_bank_leds_show_where_it_can_go(enc):
    enc.drv.tick()
    leds = enc.leds()
    assert leds[2] == 0 and leds[3] == 127      # page 0: next only
    press(enc, BANK_ZYN + 1)
    press(enc, BANK_ZYN + 1)
    enc.drv.tick()
    leds = enc.leds()
    assert leds[2] == 127 and leds[3] == 0


def test_device_mode_pages_by_eight(enc):
    m = enc.drv.mapping
    enc.dev.set_mode(ENC_ZYN, False)
    enc.dev.set_mode(ENC_DEV, True)
    assert enc.drv.device.layers_on == {ENC_DEV}
    assert m.device_page_step() == 4            # device_param_1..8 live
    turn(enc, DP_DEV + 4, 1)                    # device_param_5 on page 0: Filter / cutoff
    assert zc(enc, "cutoff").value == 65
    press(enc, BANK_DEV + 1)                    # by 4 pages of 2
    assert m.device_bank == 4
    turn(enc, DP_DEV, 1)                        # device_param_1: Env / attack
    assert zc(enc, "attack").value == 1
    press(enc, BANK_DEV + 1)                    # clamped
    assert m.device_bank == 4
    press(enc, BANK_DEV)
    assert m.device_bank == 0


def test_page_step_from_liveness():
    for modes, step in (((), 1), ((ENC_MIDI,), 1), ((ENC_ZYN,), 2), ((ENC_DEV,), 4), ((ENC_ZYN, ENC_DEV), 4)):
        r = enc_rig(modes)
        assert r.drv.mapping.device_page_step() == step, modes
        r.drv.end()


def test_role_on_two_twins_works_from_either(enc):
    turn(enc, DP_ZYN, 1)                        # device_param_1, enc_zyn twin (E5)
    enc.dev.set_mode(ENC_ZYN, False)
    enc.dev.set_mode(ENC_DEV, True)
    turn(enc, DP_DEV, 1)                        # device_param_1, enc_device twin (E1)
    assert zc(enc, "volume").value == 102


def test_page_resets_when_the_processor_or_chain_changes(enc):
    m = enc.drv.mapping
    press(enc, BANK_ZYN + 1)
    press(enc, BANK_ZYN + 1)
    assert m.device_bank == 4
    enc.chain(1).current_processor = synth()            # another processor (same id here: 7)
    enc.chain(1).current_processor.id = 8
    enc.drv.tick()
    assert m.device_bank == 0
    press(enc, BANK_ZYN + 1)
    enc.chain(2).current_processor = synth()
    enc.sm.chain_manager.set_active_chain_by_id(2)
    enc.drv.on_active_chain(2)
    enc.drv.flush()
    assert m.device_bank == 0
    turn(enc, DP_ZYN, 1)
    assert zc(enc, "volume", chain=2).value == 101 and zc(enc, "volume", chain=1).value == 100


def test_page_clamped_when_the_screens_shrink(enc):
    m = enc.drv.mapping
    press(enc, BANK_ZYN + 1)
    press(enc, BANK_ZYN + 1)
    del enc.chain(1).current_processor.screens["Env"]
    enc.drv.tick()
    assert m.device_bank == 2


def test_no_processor_maps_nothing():
    r = enc_rig(proc=False)
    r.dev.take()
    turn(r, DP_ZYN, 1)
    press(r, BANK_ZYN + 1)
    assert r.drv.mapping.device_bank == 0 and r.sm.cuias == []
    r.drv.end()


def test_param_display_shows_the_controllers_name_and_value(enc):
    enc.dev.take()
    turn(enc, DP_ZYN + 1, 1)                    # tune 65
    enc.drv.flush()
    assert _texts(enc.dev.take()) == [(0, 0, 0, "Tun 65")]
    turn(enc, DP_ZYN + 2, 1)                    # osc: its label
    enc.drv.flush()
    assert _texts(enc.dev.take()) == [(0, 0, 0, "Pulse ")]    # no room for the name
    turn(enc, DP_ZYN + 3, 10)                   # glide: a float
    enc.drv.flush()
    assert _texts(enc.dev.take()) == [(0, 0, 0, "G 0.60")]
    zc(enc, "volume").value = 127
    turn(enc, DP_ZYN, 100)                      # clamped at 127
    assert zc(enc, "volume").value == 127
    enc.drv.flush()
    assert _texts(enc.dev.take()) == [(0, 0, 0, "Vo 127")]
    for _ in range(ZM.TEXT_HOLD_TICKS):
        enc.drv.tick()
    assert _texts(enc.dev.take()) == [(0, 0, 0, "Pianot")]


def test_device_bank_shows_the_first_parameter_or_the_processor(enc):
    enc.dev.take()
    press(enc, BANK_ZYN + 1)
    assert _texts(enc.dev.take()) == [(0, 0, 0, "Cutoff")]
    enc.chain(1).current_processor.screens = OrderedDict()
    enc.drv.tick()
    enc.dev.take()
    press(enc, BANK_ZYN)
    assert _texts(enc.dev.take()) == [(0, 0, 0, "OB-Xd ")]


def test_absolute_twins_get_the_value_and_follow_the_page(enc):
    vals = dict(_values(enc.dev.take(), P.KIND_ENCODER))
    assert vals[DP_ZYN] == round(100 / 127 * P.U14_MAX)         # absolute encoder: FB_VALUE
    press(enc, BANK_ZYN + 1)
    vals = dict(_values(enc.dev.take(), P.KIND_ENCODER))
    assert vals[DP_ZYN] == round(64 / 127 * P.U14_MAX)          # now Filter / cutoff
    enc.drv.tick()
    assert not _values(enc.dev.take(), P.KIND_ENCODER)          # change-only


def test_encoder_value_echo_waits_for_the_flush(enc):
    enc.dev.take()
    for _ in range(10):                                         # a fast turn: 10 EVENTs
        turn(enc, DP_ZYN, 1)
    assert zc(enc, "volume").value == 110
    assert not _values(enc.dev.take(), P.KIND_ENCODER)          # no FB_VALUE per EVENT
    enc.drv.flush()
    v = round(110 / 127 * P.U14_MAX)
    assert _values(enc.dev.take(), P.KIND_ENCODER) == [(DP_ZYN, v), (DP_DEV, v)]   # both device_param_1 twins


def test_device_param_on_a_fader_sets_the_value():
    comps = [C(P.KIND_ANALOG, 0, "device_param_3", takeover=P.TAKEOVER_NONE),
             C(P.KIND_ANALOG, 1, "device_param_1", takeover=P.TAKEOVER_NONE)]
    r = Rig(components=comps)
    r.chain(1).current_processor = synth()
    r.drv.init()
    r.event(P.KIND_ANALOG, 0, P.U14_MAX * 3 // 4)              # osc: snapped to a tick
    r.event(P.KIND_ANALOG, 1, P.U14_MAX // 2)
    assert zc(r, "osc").value == 2 and zc(r, "volume").value == 63
    r.drv.end()


def test_read_only_and_file_controllers_are_left_alone(enc):
    p = enc.chain(1).current_processor
    p.screens["Main"][0].readonly = True
    p.screens["Main"][1] = z("sample", value="/x/kick.wav", is_path=True)
    turn(enc, DP_ZYN, 1)
    turn(enc, DP_ZYN + 1, 1)
    assert zc(enc, "volume").value == 100 and zc(enc, "sample").nudges == []
    assert zc(enc, "tune") is None and enc.drv.song.device_params()[1].zctrl is zc(enc, "sample")
    assert ZM._zctrl_value_text(zc(enc, "sample")) == "kick"


def test_old_firmware_without_layers_counts_everything_live():
    r = enc_rig(layers=False)
    assert all(c.layer is None for c in r.drv.device.all_components())
    assert r.drv.device.layers_on == set()
    assert r.drv.mapping.device_page_step() == 4                # device_param_1..8 all count
    turn(r, DP_ZYN, 1)
    turn(r, DP_DEV + 7, 1)                                      # device_param_8: Filter's gap
    assert zc(r, "volume").value == 101
    r.drv.end()


def test_old_firmware_four_device_params_page_by_a_screen():
    comps = [C(P.KIND_ENCODER, i, "device_param_%d" % (i + 1), ring=0, mode=P.ENC_REL_TWOS) for i in range(4)] + [
        C(P.KIND_BUTTON, 0, "device_bank_next")]
    r = Rig(components=comps)
    r.dev.replay_layers = False
    r.chain(1).current_processor = synth()
    r.drv.init()
    press(r, 0)
    assert r.drv.mapping.device_bank == 2
    r.drv.end()


def test_layer_event_refreshes_and_is_not_a_control(enc):
    enc.dev.take()
    enc.dev.set_mode(ENC_ZYN, False)
    assert enc.sm.cuias == [] and enc.drv.mapping.device_page_step() == 1
    enc.dev.set_mode(ENC_DEV, True)
    assert enc.drv.mapping.device_page_step() == 4


# ---- device_preset, device_preset_bank (the Code 61's ZYN mode since 0.2.107: E5, E6) ----

class FakePresetProcessor(FakeProcessor):
    """synth() with zynthian_processor's bank / preset lists: entries [id, ..., name], a None id
    is a separator, '*FAVS*' the favourites entry."""

    BANKS = {"Pads": ["Warm", None, "Glass", "Choir"], "Keys": ["Grand", "EP"], "Bass": ["Sub"]}

    def __init__(self):
        FakeProcessor.__init__(self, 7, "OB-Xd", synth().screens)
        self.bank_list = [["*FAVS*", 0, "*** Favorites ***"], ["pads", 0, "Pads"], [None, 0, "-- user --"],
                          ["keys", 0, "Keys"], ["bass", 0, "Bass"]]
        self.bank_index, self.preset_index, self.show_fav_presets = 1, 0, False
        self.calls = []
        self.load_preset_list()

    def get_bank_index(self):
        return self.bank_index

    def get_preset_index(self):
        return self.preset_index

    def set_bank(self, i):
        self.calls.append(("bank", i))
        self.bank_index = i
        return True

    def set_show_fav_presets(self, on):
        self.show_fav_presets = on

    def load_preset_list(self):
        names = self.BANKS[self.bank_list[self.bank_index][2]]
        self.preset_list = [[None if n is None else n.lower(), 0, n or "-- sep --"] for n in names]

    def set_preset(self, i):
        self.calls.append(("preset", i))
        self.preset_index = i
        return True


PRESET, PRESET_BANK, ZDP = 12, 13, 14       # encoder indices of the enc_zyn twins of E5, E6, E7


def code61_zyn_components():
    """The Code 61's enc_zyn step since 0.2.107: E1-E4 zynpots, E5 device_preset, E6
    device_preset_bank, E7-E8 device_param_1..2; Bank left / right its device_bank_prev / next."""
    B, E = P.KIND_BUTTON, P.KIND_ENCODER
    enc = lambda role, layer: C(E, 0, role, ring=0, mode=P.ENC_ABSOLUTE, layer=layer)
    return ([C(B, 0, "shift", layer=0), C(B, 1, "nav_back", layer=0),
             C(B, 2, "bank_left", led=0, layer=0), C(B, 3, "bank_right", led=1, layer=0),
             C(B, 4, "device_bank_prev", led=2, layer=ENC_ZYN + 1),
             C(B, 5, "device_bank_next", led=3, layer=ENC_ZYN + 1)] +
            [enc("", 0) for _ in range(8)] +
            [enc("zynthian.zynpot_%d" % n, ENC_ZYN + 1) for n in range(1, 5)] +
            [enc("device_preset", ENC_ZYN + 1), enc("device_preset_bank", ENC_ZYN + 1),
             enc("device_param_1", ENC_ZYN + 1), enc("device_param_2", ENC_ZYN + 1)] +
            [C(P.KIND_LED, i, "", colours=1, owner=P.OWNER_HOST_THEN_LOCAL, layer=0) for i in range(4)] +
            [C(P.KIND_DISPLAY, 0, "param", cols=6, rows=1, display_type=1)])


@pytest.fixture
def zyn():
    r = Rig(components=code61_zyn_components())
    r.dev.modes_on = {ENC_ZYN}
    r.dev.replay_layers = True
    r.chain(1).current_processor = FakePresetProcessor()
    now = [100.0]
    r.drv.song.clock = lambda: now[0]
    r.now = now
    r.drv.init()
    yield r
    r.drv.end()


def rest(rig, seconds=0.3):
    rig.now[0] += seconds
    rig.drv.tick()


def test_preset_roles_are_known(zyn):
    roles = [r for _k, _i, r in zyn.drv.mapping.unknown_roles]
    assert "device_preset" not in roles and "device_preset_bank" not in roles


def test_preset_encoder_steps_and_loads_once_it_rests(zyn):
    proc = zyn.chain(1).current_processor
    zyn.dev.take()
    turn(zyn, PRESET, 1)                        # Warm -> (separator) -> Glass
    zyn.drv.flush()
    assert _texts(zyn.dev.take()) == [(0, 0, 0, "Glass ")]
    assert proc.calls == []                     # not loaded while it turns
    turn(zyn, PRESET, 1)                        # Choir
    zyn.now[0] += 0.1
    zyn.drv.tick()
    assert proc.calls == []                     # not rested long enough
    rest(zyn)
    assert proc.calls == [("preset", 3)] and proc.preset_index == 3
    assert ("refresh_screen", ["control", "bank", "preset", "mixer"]) in zyn.sm.cuias
    turn(zyn, PRESET, 5)                        # clamped at the last preset: nothing to load
    rest(zyn)
    assert proc.calls == [("preset", 3)]
    turn(zyn, PRESET, -9)                       # back to the first, the separator skipped
    rest(zyn)
    assert proc.calls[-1] == ("preset", 0)


def test_bank_encoder_picks_the_bank_and_its_first_preset(zyn):
    proc = zyn.chain(1).current_processor
    zyn.dev.take()
    turn(zyn, PRESET_BANK, 1)                   # Pads -> (user separator) -> Keys
    zyn.drv.flush()
    assert _texts(zyn.dev.take()) == [(0, 0, 0, "Keys  ")]
    rest(zyn)
    assert proc.calls == [("bank", 3), ("preset", 0)]
    assert [e[2] for e in proc.preset_list] == ["Grand", "EP"]
    turn(zyn, PRESET_BANK, -5)                  # back to Pads; the favourites entry is skipped
    rest(zyn)
    assert proc.calls[-2:] == [("bank", 1), ("preset", 0)]


def test_preset_pick_is_dropped_when_the_processor_changes(zyn):
    proc = zyn.chain(1).current_processor
    turn(zyn, PRESET, 1)
    zyn.chain(1).current_processor = FakePresetProcessor()
    zyn.chain(1).current_processor.id = 8
    rest(zyn)
    assert proc.calls == [] and zyn.chain(1).current_processor.calls == []


def test_zyn_mode_pages_two_parameters(zyn):
    m = zyn.drv.mapping
    assert m.device_page_step() == 1            # device_param_1..2 live: pages of 2
    turn(zyn, ZDP + 1, -4)                      # E8: device_param_2 = tune
    assert zc(zyn, "tune").value == 60
    press(zyn, 5)                               # Bank right: device_bank_next
    turn(zyn, ZDP, 1)                           # E7 now Main / osc
    turn(zyn, ZDP + 1, 10)                      # E8 Main / glide
    assert zc(zyn, "osc").value == 1 and zc(zyn, "glide").value == pytest.approx(0.6)
    press(zyn, 5)
    turn(zyn, ZDP, 1)                           # Filter / cutoff
    assert zc(zyn, "cutoff").value == 65
    for _ in range(5):
        press(zyn, 5)                           # clamped at the last pair (Env / sustain, release)
    assert m.device_bank == 5
    turn(zyn, ZDP + 1, 3)
    assert zc(zyn, "release").value == 3


def test_no_processor_the_preset_encoders_do_nothing(zyn):
    zyn.chain(1).current_processor = None
    zyn.drv.tick()
    turn(zyn, PRESET, 1)
    turn(zyn, PRESET_BANK, 1)
    rest(zyn)
    assert not [c for c in zyn.sm.cuias if c[0] == "refresh_screen"]
