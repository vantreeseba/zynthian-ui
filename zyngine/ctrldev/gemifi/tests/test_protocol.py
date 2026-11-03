# SPDX-License-Identifier: MIT
# Copied from gemifi (hosts/ableton-live/tests/test_protocol.py), MIT licensed; see ../LICENSE-MIT.
import json
import os

import pytest

from gemifi import protocol as P

VECTORS = os.path.join(os.path.dirname(__file__), "test-vectors.json")


def hx(s):
    return bytes.fromhex(s.replace(" ", ""))


@pytest.fixture(scope="module")
def vectors():
    with open(VECTORS) as f:
        return json.load(f)


def _frame(vectors, start):
    for v in vectors["frames"]:
        if v["desc"].startswith(start):
            return v
    raise KeyError(start)


def test_hello_request_vectors(vectors):
    for desc in ("HELLO request", "HELLO request seq 5"):
        v = [x for x in vectors["frames"] if x["desc"] == desc or x["desc"].startswith(desc + " (")][0]
        assert P.build_frame(v["cmd"], v["seq"], hx(v["payload"])) == hx(v["frame"])


def test_hello_reply_vector_app(vectors):
    v = _frame(vectors, "HELLO reply from an app")
    f = P.parse_frame(hx(v["frame"]))
    assert (f.cmd, f.seq) == (P.HELLO | P.REPLY, v["seq"])
    h = P.parse_hello(f.payload)
    assert h.role == 0 and h.board_id == 1
    assert h.version_text == "0.1.5"
    assert h.node == "keystation-mini32"
    assert h.uid.hex().upper() == "1032547698BADCFE01234567"
    assert not h.has_feedback            # that firmware predates bit 6
    assert P.build_frame(f.cmd, f.seq, P.build_hello_reply(h)) == hx(v["frame"])


def test_hello_reply_vector_bootloader(vectors):
    v = _frame(vectors, "HELLO reply from bootloader")
    h = P.parse_hello(P.parse_frame(hx(v["frame"])).payload)
    assert h.role == 1 and h.node is None and h.caps == P.CAP_FW


def test_event_vector(vectors):
    v = _frame(vectors, "EVENT key")
    f = P.parse_frame(hx(v["frame"]))
    ev = P.parse_event(f.payload)
    assert (ev.type, ev.index, ev.value, ev.t_ms) == (2, 12, 100, 70000)
    assert P.build_event(2, 12, 100, 70000) == hx(v["payload"])


def test_all_vector_frames_parse_and_rebuild(vectors):
    for v in vectors["frames"]:
        f = P.parse_frame(hx(v["frame"]))
        assert P.build_frame(f.cmd, f.seq, f.payload) == hx(v["frame"]), v["desc"]


def test_bad_frames_rejected(vectors):
    for v in vectors.get("bad_frames", []):
        with pytest.raises(P.ProtocolError):
            P.parse_frame(hx(v["frame"]))


def test_int_vectors(vectors):
    for v in vectors["u14"]:
        assert P.u14(v["value"]) == hx(v["bytes"])
        assert P.dec_u14(hx(v["bytes"])) == v["value"]
    for v in vectors["u28"]:
        assert P.u28(v["value"]) == hx(v["bytes"])
    for v in vectors["u32"]:
        assert P.u32(v["value"]) == hx(v["bytes"])
        assert P.dec_u32(hx(v["bytes"])) == v["value"]


def test_pack7_vectors(vectors):
    for v in vectors["pack7"]:
        assert P.pack7(hx(v["raw"])) == hx(v["packed"])
        assert P.unpack7(hx(v["packed"])) == hx(v["raw"])


def test_s32_and_s14():
    assert P.s32(-1) == hx("7F 7F 7F 7F 0F")
    assert P.dec_s32(P.s32(-123456)) == -123456
    for d in (-8192, -3, -1, 0, 1, 5, 8191):
        assert P.s14_to_int(P.int_to_s14(d)) == d
    assert P.s14_to_int(0x3FFF) == -1


def test_lstring():
    enc = P.enc_lstring("fader_1")
    assert enc == bytes([7]) + b"fader_1"
    assert P.dec_lstring(enc + b"xx", 0) == ("fader_1", 8)
    assert P.enc_lstring("x" * 40)[0] == 31
    with pytest.raises(P.ProtocolError):
        P.dec_lstring(bytes([5, 0x41]), 0)
    with pytest.raises(P.ProtocolError):
        P.dec_lstring(bytes([32]) + b"a" * 32, 0)


@pytest.mark.parametrize("c", [
    P.Component(P.KIND_BUTTON, 3, 9, "btn_play", "Play", "play", led=7),
    P.Component(P.KIND_BUTTON, 0, 1, "b", "", "", led=P.NONE_INDEX),
    P.Component(P.KIND_KEYS, 0, 1, "keys", "Keys", "", keys=32, first_note=48),
    P.Component(P.KIND_ANALOG, 200, 300, "f1", "Fader 1", "fader_1"),
    P.Component(P.KIND_ENCODER, 1, 8, "e2", "Enc 2", "device_param_2", ring=11, center=40),
    P.Component(P.KIND_LED, 9, 10, "led", "", "record", colours=2),
    P.Component(P.KIND_DISPLAY, 0, 1, "lcd", "LCD", "", cols=16, rows=2, display_type=1),
])
def test_component_round_trip(c):
    payload = P.build_component_reply(c)
    frame = P.build_frame(P.GET_COMPONENT | P.REPLY, 9, payload)
    got = P.parse_component_reply(P.parse_frame(frame).payload)
    for attr in ("kind", "index", "count", "id", "name", "role", "led", "keys", "first_note", "ring",
                 "center", "colours", "cols", "rows", "display_type"):
        assert getattr(got, attr) == getattr(c, attr), attr


def test_component_reply_bytes():
    c = P.Component(P.KIND_BUTTON, 2, 5, "rec", "Rec", "record", led=2)
    assert P.build_component_reply(c) == (
        hx("00 01 02 00 05 00") + b"\x03rec" + b"\x03Rec" + b"\x06record" + hx("02 00"))
    with pytest.raises(P.ProtocolError):     # extra fields missing
        P.parse_component_reply(P.build_component_reply(c)[:-2])


def test_get_component_request():
    assert P.build_frame(P.GET_COMPONENT, 3, P.req_get_component(4, 130)) == \
        hx("F0 7D 67 6D 01 11 03 03 00 04 02 01") + bytes([(1 + 0x11 + 3 + 3 + 4 + 2 + 1) & 0x7F, 0xF7])


def test_set_param_request():
    assert P.req_set_param(13, 0, 1) == hx("0D 00 00 00 01 00 00 00 00")
    assert P.parse_param_reply(P.build_param_reply(13, 0, 1)) == (13, 0, 1)


def test_fb_payloads():
    assert P.fb_led(130, 127) == hx("02 01 7F")
    assert P.fb_led_rgb(1, 127, 0, 5, blink=True) == hx("01 00 7F 00 05 01")
    assert P.fb_leds(8, [0, 1, 127]) == hx("08 00 03 00 01 7F")
    assert P.fb_value(3, 2, 16383) == hx("03 02 00 7F 7F")
    assert P.fb_ring(1, P.RING_BOOST_CUT, 8192, True) == hx("01 00 01 00 40 01")
    assert P.fb_text(0, 1, 2, "Hi\n") == hx("00 00 01 02 48 69 20")
    assert P.fb_release() == hx("00")
    with pytest.raises(ValueError):
        P.fb_value(1, 0, 0)
    with pytest.raises(ValueError):
        P.fb_leds(0, [])
    with pytest.raises(ValueError):
        P.fb_led(0, 128)


def test_fb_frames_are_notifications():
    frame = P.build_frame(P.FB_RELEASE, 0, P.fb_release(0))
    assert frame == hx("F0 7D 67 6D 01 26 00 01 00 00 28 F7")
    f = P.parse_frame(frame)
    assert not f.is_reply and not f.is_unsolicited


def test_added_component_fields():
    """takeover, mode, owner (sysex.md 4.6.1): one byte each after the kind's fields, optional."""
    a = P.Component(P.KIND_ANALOG, 0, 1, "", "", "", takeover=P.TAKEOVER_PICKUP)
    assert P.build_component_reply(a) == hx("00 03 00 00 01 00 00 00 00 01")
    e = P.Component(P.KIND_ENCODER, 0, 1, "", "", "", ring=5, center=200, mode=P.ENC_REL_OFFSET)
    assert P.build_component_reply(e)[-4:] == hx("05 48 01 02")
    x = P.Component(P.KIND_LED, 0, 1, "", "", "", colours=2, owner=P.OWNER_LOCAL)
    assert P.build_component_reply(x)[-2:] == hx("02 02")
    for c in (a, e, x):
        got = P.parse_component_reply(P.build_component_reply(c))
        assert (got.takeover, got.mode, got.owner) == (c.takeover, c.mode, c.owner)
        assert got.reports_added_fields
    old = P.parse_component_reply(P.build_component_reply(P.Component(P.KIND_ENCODER, 0, 1, ring=3)))
    assert old.mode is None and old.takes_value and not old.reports_added_fields
    assert not P.Component(P.KIND_ANALOG, 0, 1, takeover=P.TAKEOVER_NONE).takes_value
    assert not P.Component(P.KIND_ENCODER, 0, 1, mode=P.ENC_MCU).takes_value
    assert P.Component(P.KIND_LED, 0, 1, owner=P.OWNER_LOCAL).local_only
    assert not P.Component(P.KIND_LED, 0, 1).local_only


def test_feedback_vectors(vectors):
    """The GET_COMPONENT replies and BOOTED in protocol/test-vectors.json's feedback section."""
    seen = set()
    for v in vectors["feedback"]["frames"]:
        f = P.parse_frame(hx(v["frame"]))
        if f.cmd == P.GET_COMPONENT | P.REPLY and f.payload[0] == P.OK:
            c = P.parse_component_reply(f.payload)
            fields = v["fields"]
            for attr in ("takeover", "mode", "owner"):
                if attr in fields:
                    assert getattr(c, attr) == fields[attr], v["desc"]
                    seen.add((attr, fields[attr] is None))
            assert P.build_component_reply(c) == f.payload, v["desc"]
        elif f.cmd == P.BOOTED:
            h = P.parse_booted(f.payload)
            assert (h.board_id, h.fw_version, h.caps, h.node) == (
                v["fields"]["board_id"], v["fields"]["fw_version"], v["fields"]["caps"], v["fields"]["node"])
            assert h.has_feedback
            seen.add("booted")
    assert {"booted", ("takeover", False), ("takeover", True), ("mode", False), ("mode", True),
            ("owner", False), ("owner", True)} <= seen
