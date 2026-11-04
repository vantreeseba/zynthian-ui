# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Benjamin Van Treese
# Copied from gemifi (hosts/ableton-live/gemifi/protocol.py), MIT licensed, with pads (kind 7),
# meters (kind 8), the analog touch byte and EVENT types 5 / 7 added (gemifi issue #21).
# See LICENSE-MIT in this directory. The rest of the gemifi driver is GPL.
"""gemifi SysEx protocol v1 codec, the parts a DAW script needs (protocol/sysex.md).

Pure Python, no Live imports, Python 3.7+ syntax. The encoding helpers follow the
reference codec in tools/host/gemifi_proto.py (this project's own MIT code).

Frame:  F0 7D 67 6D  ver cmd seq len_lo len_hi  payload...  chk  F7
chk = (sum of ver..last payload byte) & 0x7F; len = payload length in SysEx bytes (u14).
"""

MANUFACTURER = (0x7D,)          # MIDI non-commercial ID (sysex.md 1); one constant, as the spec asks
SIGNATURE = (0x67, 0x6D)        # 'g' 'm'
HEADER = (0xF0,) + MANUFACTURER + SIGNATURE
PROTO_VERSION = 1

# Requests 0x00-0x1F, replies cmd|0x40, host->device notifications 0x20-0x3F, unsolicited 0x60-0x7F.
HELLO = 0x01
GET_PARAM = 0x03
SET_PARAM = 0x04
GET_COMPONENT = 0x11
REPLY = 0x40
NOTIFY_FIRST, NOTIFY_LAST = 0x20, 0x3F
UNSOLICITED_FIRST = 0x60
LOG = 0x60
EVENT = 0x61
PARAM_CHANGED = 0x62
BOOTED = 0x64                   # sysex.md 4.6.3: HELLO's fields, sent once after the device starts

# Host feedback notifications (sysex.md 4.6.2). Never answered.
FB_LED = 0x20
FB_LED_RGB = 0x21
FB_LEDS = 0x22
FB_VALUE = 0x23
FB_RING = 0x24
FB_TEXT = 0x25
FB_RELEASE = 0x26
FB_METER = 0x27                 # reserved: current firmware drops it

RELEASE_ALL, RELEASE_LEDS, RELEASE_VALUES, RELEASE_TEXT = 0, 1, 2, 3
RING_DOT, RING_BOOST_CUT, RING_WRAP, RING_SPREAD = 0, 1, 2, 3

(OK, BAD_CHECKSUM, BAD_CMD, BAD_LEN, BAD_STATE, BAD_ADDR, FLASH_ERR, BAD_IMAGE, BAD_BOARD, BUSY,
 BAD_PARAM, BAD_VALUE) = range(12)
STATUS_NAMES = ("OK", "BAD_CHECKSUM", "BAD_CMD", "BAD_LEN", "BAD_STATE", "BAD_ADDR", "FLASH_ERR",
                "BAD_IMAGE", "BAD_BOARD", "BUSY", "BAD_PARAM", "BAD_VALUE")

CAP_FW, CAP_CONFIG, CAP_LOG, CAP_EVENT = 1, 2, 4, 8
CAP_PRESETS = 1 << 5
CAP_FEEDBACK = 1 << 6           # sysex.md 4.1 bit 6

PARAM_DEBUG_EVENTS = 13         # sysex.md 4.3 / 4.6 step 3

# Component kinds (sysex.md 4.6.1); 1..4 and 7 are also EVENT types. Firmware from before pads
# and meters answers BAD_PARAM for kinds 7 and 8, which enumeration reads as "none of that kind".
KIND_BUTTON, KIND_KEYS, KIND_ANALOG, KIND_ENCODER, KIND_LED, KIND_DISPLAY = 1, 2, 3, 4, 5, 6
KIND_PAD, KIND_METER = 7, 8
ALL_KINDS = (KIND_BUTTON, KIND_KEYS, KIND_ANALOG, KIND_ENCODER, KIND_LED, KIND_DISPLAY, KIND_PAD, KIND_METER)
KIND_NAMES = {1: "button", 2: "keys", 3: "analog", 4: "encoder", 5: "led", 6: "display", 7: "pad", 8: "meter"}

# EVENT types (daw-scripts.md 4.5): the kinds above, plus touch.
EV_BUTTON, EV_KEY, EV_ANALOG, EV_ENCODER, EV_TOUCH, EV_PAD = 1, 2, 3, 4, 5, 7
NONE_INDEX = 0x3FFF             # "no LED" in GET_COMPONENT replies

# Fields added to GET_COMPONENT after the first F3 firmware (sysex.md 4.6.1 "Added fields").
# A Component holds None for one the device didn't send.
TAKEOVER_NONE, TAKEOVER_PICKUP, TAKEOVER_MOTOR = 0, 1, 2   # 2 reserved: FB_VALUE moves the fader
TOUCH_NONE, TOUCH_SENSOR, TOUCH_MOVEMENT = 0, 1, 2            # analog `touch` byte
METER_LED_BAR, METER_RING, METER_DISPLAY_ROW = 1, 2, 3        # meter `type` byte (reserved)
ENC_ABSOLUTE, ENC_REL_TWOS, ENC_REL_OFFSET, ENC_MCU = 0, 1, 2, 3
OWNER_HOST_THEN_LOCAL, OWNER_HOST, OWNER_LOCAL = 0, 1, 2

U14_MAX = 0x3FFF


class ProtocolError(ValueError):
    pass


# ---- 7-bit integer encodings (least significant group first) ----

def _enc(value, nbytes, bits):
    value = int(value)
    if not 0 <= value < (1 << bits):
        raise ValueError("%d does not fit in %d bits" % (value, bits))
    return bytes((value >> (7 * i)) & 0x7F for i in range(nbytes))


def _dec(data, nbytes):
    if len(data) < nbytes:
        raise ProtocolError("need %d bytes, got %d" % (nbytes, len(data)))
    v = 0
    for i in range(nbytes):
        b = data[i]
        if b & 0x80:
            raise ProtocolError("byte with bit 7 set inside SysEx")
        v |= b << (7 * i)
    return v


def u14(v):
    return _enc(v, 2, 14)


def u28(v):
    return _enc(v, 4, 28)


def u32(v):
    return _enc(v, 5, 32)


def s32(v):
    """Signed 32-bit as the u32 of its two's-complement pattern (-1 -> 7F 7F 7F 7F 0F)."""
    if not -(1 << 31) <= v < (1 << 31):
        raise ValueError("%d does not fit in s32" % v)
    return u32(v & 0xFFFFFFFF)


def dec_u14(d):
    return _dec(d, 2)


def dec_u28(d):
    return _dec(d, 4)


def dec_u32(d):
    v = _dec(d, 5)
    if v >> 32:
        raise ProtocolError("u32 top byte has bits above bit 3 set")
    return v


def dec_s32(d):
    v = dec_u32(d)
    return v - (1 << 32) if v & 0x80000000 else v


def s14_to_int(v):
    """A 14-bit two's-complement value (encoder EVENT delta) as a Python int."""
    v &= U14_MAX
    return v - 0x4000 if v & 0x2000 else v


def int_to_s14(v):
    if not -0x2000 <= v < 0x2000:
        raise ValueError("%d does not fit in s14" % v)
    return v & U14_MAX


# ---- 8->7 packing (Korg/Elektron: MSB byte first, then up to 7 low-7-bit bytes) ----

def pack7(data):
    data = bytes(data)
    out = bytearray()
    for i in range(0, len(data), 7):
        group = data[i:i + 7]
        msbs = 0
        for j, b in enumerate(group):
            msbs |= ((b >> 7) & 1) << j
        out.append(msbs)
        out.extend(b & 0x7F for b in group)
    return bytes(out)


def unpack7(data):
    data = bytes(data)
    out = bytearray()
    for i in range(0, len(data), 8):
        group = data[i:i + 8]
        if len(group) < 2:
            raise ProtocolError("packed data ends with a lone MSB byte")
        if any(b & 0x80 for b in group):
            raise ProtocolError("byte with bit 7 set inside SysEx")
        msbs = group[0]
        for j, b in enumerate(group[1:]):
            out.append(b | (((msbs >> j) & 1) << 7))
    return bytes(out)


# ---- lstring (sysex.md 2: u8 len 0..31, printable ASCII, unpacked) ----

LSTRING_MAX = 31


def enc_lstring(text):
    raw = text.encode("ascii", "replace")[:LSTRING_MAX]
    raw = bytes(b if 0x20 <= b <= 0x7E else 0x3F for b in raw)
    return bytes([len(raw)]) + raw


def dec_lstring(data, pos):
    """Return (text, new_pos). Non-printable bytes come back as '?'."""
    if pos >= len(data):
        raise ProtocolError("lstring: missing length byte")
    n = data[pos]
    if n > LSTRING_MAX:
        raise ProtocolError("lstring length %d > %d" % (n, LSTRING_MAX))
    end = pos + 1 + n
    if end > len(data):
        raise ProtocolError("lstring runs past the payload")
    text = "".join(chr(b) if 0x20 <= b <= 0x7E else "?" for b in data[pos + 1:end])
    return text, end


# ---- framing ----

def checksum(body):
    return sum(body) & 0x7F


def build_frame(cmd, seq, payload=b"", ver=PROTO_VERSION):
    """Return the complete SysEx message as bytes."""
    payload = bytes(payload)
    if any(b & 0x80 for b in payload):
        raise ValueError("payload must be 7-bit")
    if not 0 <= seq <= 0x7F or not 0 <= cmd <= 0x7F:
        raise ValueError("cmd and seq must be 0..127")
    body = bytes([ver, cmd, seq]) + u14(len(payload)) + payload
    return bytes(HEADER) + body + bytes([checksum(body), 0xF7])


class Frame(object):
    __slots__ = ("ver", "cmd", "seq", "payload")

    def __init__(self, ver, cmd, seq, payload=b""):
        self.ver, self.cmd, self.seq, self.payload = ver, cmd, seq, bytes(payload)

    @property
    def is_reply(self):
        return REPLY <= self.cmd < UNSOLICITED_FIRST

    @property
    def is_unsolicited(self):
        return self.cmd >= UNSOLICITED_FIRST

    @property
    def status(self):
        return self.payload[0] if self.payload else None

    def __repr__(self):
        return "Frame(cmd=0x%02X seq=%d payload=%s)" % (self.cmd, self.seq, self.payload.hex())


def is_gemifi(msg):
    return len(msg) >= len(HEADER) and tuple(msg[:len(HEADER)]) == HEADER


def parse_frame(msg):
    """Parse one complete SysEx message (F0..F7). Raises ProtocolError."""
    msg = bytes(msg)
    h = len(HEADER)
    if len(msg) < h + 7 or msg[0] != 0xF0 or msg[-1] != 0xF7:
        raise ProtocolError("not a complete SysEx message")
    if tuple(msg[:h]) != HEADER:
        raise ProtocolError("not a gemifi message")
    inner = msg[1:-1]
    if any(b & 0x80 for b in inner):
        raise ProtocolError("byte with bit 7 set inside SysEx")
    body, chk = msg[h:-2], msg[-2]
    ver, cmd, seq = body[0], body[1], body[2]
    n = dec_u14(body[3:5])
    payload = body[5:]
    if len(payload) != n:
        raise ProtocolError("length field %d but payload has %d bytes" % (n, len(payload)))
    if checksum(body) != chk:
        raise ProtocolError("bad checksum")
    if ver != PROTO_VERSION:
        raise ProtocolError("unsupported protocol version %d" % ver)
    return Frame(ver, cmd, seq, payload)


# ---- HELLO ----

HELLO_REPLY_LEN = 1 + 1 + 2 + 5 + 1 + 2 + 2 + 14   # status + fields, without the optional node name


class Hello(object):
    def __init__(self, role, board_id, fw_version, proto_version, max_chunk, caps, uid=b"", node=None):
        self.role = role
        self.board_id = board_id
        self.fw_version = fw_version
        self.proto_version = proto_version
        self.max_chunk = max_chunk
        self.caps = caps
        self.uid = bytes(uid)
        self.node = node

    @property
    def has_feedback(self):
        return bool(self.caps & CAP_FEEDBACK)

    @property
    def version_text(self):
        v = self.fw_version
        return "%d.%d.%d" % (v >> 24, (v >> 16) & 0xFF, v & 0xFFFF)


def _valid_node(name):
    if not 1 <= len(name) <= 31 or name[0] == "-" or name[-1] == "-":
        return False
    return all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name)


def parse_hello(payload):
    """HELLO reply payload (status OK) -> Hello. Extra trailing bytes are ignored (sysex.md 4.1.1)."""
    payload = bytes(payload)
    if len(payload) < HELLO_REPLY_LEN:
        raise ProtocolError("HELLO reply is %d bytes, expected at least %d" % (len(payload), HELLO_REPLY_LEN))
    p = payload[1:]
    node = None
    rest = payload[HELLO_REPLY_LEN:]
    if rest and rest[0] and 1 + rest[0] <= len(rest):
        name = rest[1:1 + rest[0]].decode("ascii", "replace")
        node = name if _valid_node(name) else None
    return Hello(role=p[0], board_id=dec_u14(p[1:3]), fw_version=dec_u32(p[3:8]), proto_version=p[8],
                 max_chunk=dec_u14(p[9:11]), caps=dec_u14(p[11:13]), uid=unpack7(p[13:27]), node=node)


def build_hello_reply(h, status=OK):
    """Device side (tests, fake device)."""
    return (bytes([status, h.role]) + u14(h.board_id) + u32(h.fw_version) + bytes([h.proto_version]) +
            u14(h.max_chunk) + u14(h.caps) + pack7(h.uid) +
            (bytes([len(h.node)]) + h.node.encode("ascii") if h.node else b""))


def parse_booted(payload):
    """BOOTED payload (sysex.md 4.6.3) -> Hello: a HELLO reply without its status byte."""
    return parse_hello(bytes([OK]) + bytes(payload))


# ---- GET_PARAM / SET_PARAM ----

def req_get_param(pid, index=0):
    return u14(pid) + u14(index)


def req_set_param(pid, index, value):
    return u14(pid) + u14(index) + s32(value)


def parse_param_reply(payload):
    """GET_PARAM / SET_PARAM / PARAM_CHANGED body -> (id, index, value). Skips the status byte if
    `payload` is a reply (10 bytes); PARAM_CHANGED has no status (9 bytes)."""
    payload = bytes(payload)
    if len(payload) == 10:
        payload = payload[1:]
    if len(payload) != 9:
        raise ProtocolError("param payload must be 9 bytes after the status")
    return dec_u14(payload[0:2]), dec_u14(payload[2:4]), dec_s32(payload[4:9])


def build_param_reply(pid, index, value, status=OK):
    if status != OK:
        return bytes([status])
    return bytes([OK]) + u14(pid) + u14(index) + s32(value)


# ---- GET_COMPONENT (sysex.md 4.6.1) ----

def req_get_component(kind, index):
    return bytes([kind]) + u14(index)


class Component(object):
    """One component from a GET_COMPONENT reply. `role` is the raw string from the device;
    the script's normalised role is set later by device.normalise_role()."""

    def __init__(self, kind, index, count, id="", name="", role="", led=NONE_INDEX, keys=0, first_note=0,
                 ring=0, center=NONE_INDEX, colours=1, cols=0, rows=0, display_type=0, takeover=None,
                 mode=None, owner=None, touch=None, segments=0, meter_type=0):
        self.kind = kind
        self.index = index
        self.count = count
        self.id = id
        self.name = name
        self.role = role
        self.led = led
        self.keys = keys
        self.first_note = first_note
        self.ring = ring
        self.center = center
        self.colours = colours
        self.cols = cols
        self.rows = rows
        self.display_type = display_type
        self.takeover = takeover        # analog: 0 none, 1 pick-up; None = not reported (older firmware)
        self.mode = mode                # encoder: 0 absolute, 1/2 relative, 3 Mackie V-Pot; None = not reported
        self.owner = owner              # LED: 0 host_then_local, 1 host, 2 local; None = not reported
        self.touch = touch              # analog: 0 none, 1 sensor, 2 from movement; None = not reported
        self.segments = segments        # meter (reserved)
        self.meter_type = meter_type    # meter: 1 LED bar, 2 encoder ring, 3 display row

    @property
    def has_led(self):
        return self.kind in (KIND_BUTTON, KIND_PAD) and self.led != NONE_INDEX

    @property
    def takes_value(self):
        """Whether FB_VALUE does anything (sysex.md 4.6.2): a pick-up analog input or an absolute
        encoder. True when the device didn't say, as the script assumed before the field existed."""
        if self.kind == KIND_ANALOG:
            return self.takeover is None or self.takeover in (TAKEOVER_PICKUP, TAKEOVER_MOTOR)
        if self.kind == KIND_ENCODER:
            return self.mode is None or self.mode == ENC_ABSOLUTE
        return False

    @property
    def local_only(self):
        """An LED with owner: local ignores every FB_* (sysex.md 4.6.2 "Ownership")."""
        return self.kind == KIND_LED and self.owner == OWNER_LOCAL

    @property
    def reports_added_fields(self):
        """Whether the reply carried the added field of its kind (so the firmware also sends BOOTED)."""
        return {KIND_ANALOG: self.takeover, KIND_ENCODER: self.mode, KIND_LED: self.owner}.get(self.kind) is not None

    def __repr__(self):
        return "Component(%s %d %r role=%r)" % (KIND_NAMES.get(self.kind, self.kind), self.index, self.id, self.role)


def parse_component_reply(payload):
    """GET_COMPONENT reply payload (status OK) -> Component."""
    payload = bytes(payload)
    if len(payload) < 6 or payload[0] != OK:
        raise ProtocolError("GET_COMPONENT reply too short or not OK")
    kind = payload[1]
    index = dec_u14(payload[2:4])
    count = dec_u14(payload[4:6])
    pos = 6
    cid, pos = dec_lstring(payload, pos)
    name, pos = dec_lstring(payload, pos)
    role, pos = dec_lstring(payload, pos)
    c = Component(kind, index, count, cid, name, role)
    rest = payload[pos:]

    def need(n):
        if len(rest) < n:
            raise ProtocolError("GET_COMPONENT %s reply missing its extra fields" % KIND_NAMES.get(kind, kind))

    if kind == KIND_BUTTON:
        need(2)
        c.led = dec_u14(rest[0:2])
    elif kind == KIND_KEYS:
        need(3)
        c.keys, c.first_note = dec_u14(rest[0:2]), rest[2]
    elif kind == KIND_ANALOG:
        c.takeover = rest[0] if len(rest) > 0 else None
        c.touch = rest[1] if len(rest) > 1 else None
    elif kind == KIND_ENCODER:
        need(3)
        c.ring, c.center = rest[0], dec_u14(rest[1:3])
        c.mode = rest[3] if len(rest) > 3 else None
    elif kind == KIND_LED:
        need(1)
        c.colours = rest[0]
        c.owner = rest[1] if len(rest) > 1 else None
    elif kind == KIND_DISPLAY:
        need(3)
        c.cols, c.rows, c.display_type = rest[0], rest[1], rest[2]
    elif kind == KIND_PAD:
        need(2)
        c.led = dec_u14(rest[0:2])
    elif kind == KIND_METER:
        need(2)
        c.segments, c.meter_type = rest[0], rest[1]
    # takeover / touch / mode / owner are absent in older firmware; bytes after the known fields
    # (and unknown kinds' fields) are ignored.
    return c


def build_component_reply(c):
    """Device side (tests, fake device)."""
    out = (bytes([OK, c.kind]) + u14(c.index) + u14(c.count) + enc_lstring(c.id) + enc_lstring(c.name) +
           enc_lstring(c.role))
    if c.kind == KIND_BUTTON:
        out += u14(c.led)
    elif c.kind == KIND_KEYS:
        out += u14(c.keys) + bytes([c.first_note])
    elif c.kind == KIND_ANALOG:
        out += b"" if c.takeover is None else bytes([c.takeover])
        out += b"" if c.takeover is None or c.touch is None else bytes([c.touch])
    elif c.kind == KIND_ENCODER:
        out += bytes([c.ring]) + u14(c.center) + (b"" if c.mode is None else bytes([c.mode]))
    elif c.kind == KIND_LED:
        out += bytes([c.colours]) + (b"" if c.owner is None else bytes([c.owner]))
    elif c.kind == KIND_DISPLAY:
        out += bytes([c.cols, c.rows, c.display_type])
    elif c.kind == KIND_PAD:
        out += u14(c.led)
    elif c.kind == KIND_METER:
        out += bytes([c.segments, c.meter_type])
    return out


# ---- EVENT (0x61) ----

class Event(object):
    __slots__ = ("type", "index", "value", "t_ms")

    def __init__(self, type, index, value, t_ms=0):
        self.type, self.index, self.value, self.t_ms = type, index, value, t_ms

    @property
    def delta(self):
        """Encoder events: `value` is a signed delta, 14-bit two's complement."""
        return s14_to_int(self.value)

    @property
    def pressed(self):
        """Buttons (1 down), pads (peak reading x 4 on a hit), touch (1 touched): not 0."""
        return self.value != 0

    def __repr__(self):
        return "Event(type=%d index=%d value=%d)" % (self.type, self.index, self.value)


T_MS_MASK = (1 << 28) - 1     # EVENT t_ms is a u28 that wraps


def t_ms_diff(later, earlier):
    """Milliseconds from `earlier` to `later` (EVENT t_ms values), across the u28 wrap."""
    return (later - earlier) & T_MS_MASK


def parse_event(payload):
    """EVENT payload -> Event. Bytes after the 9 known ones are ignored (append-only, sysex.md 1)."""
    payload = bytes(payload)
    if len(payload) < 9:
        raise ProtocolError("EVENT payload must be at least 9 bytes")
    return Event(payload[0], dec_u14(payload[1:3]), dec_u14(payload[3:5]), dec_u28(payload[5:9]))


def build_event(etype, index, value, t_ms=0):
    return bytes([etype]) + u14(index) + u14(value & U14_MAX) + u28(t_ms)


# ---- FB_* payloads (sysex.md 4.6.2) ----

def _byte(v, what):
    v = int(v)
    if not 0 <= v <= 0x7F:
        raise ValueError("%s must be 0..127, got %d" % (what, v))
    return bytes([v])


def fb_led(led, level):
    return u14(led) + _byte(level, "level")


def fb_led_rgb(led, r, g, b, blink=False):
    return u14(led) + _byte(r, "r") + _byte(g, "g") + _byte(b, "b") + bytes([1 if blink else 0])


def fb_leds(first, levels):
    levels = list(levels)
    if not 1 <= len(levels) <= 64:
        raise ValueError("FB_LEDS takes 1..64 levels")
    return u14(first) + bytes([len(levels)]) + b"".join(_byte(v, "level") for v in levels)


def fb_value(kind, index, value):
    if kind not in (KIND_ANALOG, KIND_ENCODER):
        raise ValueError("FB_VALUE kind must be 3 (analog) or 4 (encoder)")
    return bytes([kind]) + u14(index) + u14(value)


def fb_ring(encoder, mode, value, centre=False, off=False):
    """FB_RING; off (flags bit 1) turns every ring LED off."""
    return u14(encoder) + _byte(mode, "mode") + u14(value) + bytes([(1 if centre else 0) | (2 if off else 0)])


def fb_text(display, row, col, text):
    raw = text.encode("ascii", "replace")[:64]
    raw = bytes(b if 0x20 <= b <= 0x7E else 0x20 for b in raw)
    return u14(display) + _byte(row, "row") + _byte(col, "col") + raw


def fb_release(what=RELEASE_ALL):
    return _byte(what, "what")


def fb_meter(meter, level, clip=False):
    """FB_METER (reserved; no firmware draws meters yet)."""
    return u14(meter) + _byte(level, "level") + bytes([1 if clip else 0])
