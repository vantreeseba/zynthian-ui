# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Benjamin Van Treese
# Copied from gemifi (hosts/ableton-live/gemifi/device.py), MIT licensed (channel_left/right ->
# strip_prev/next, gemifi issue #21; layer modes from EVENT type 8).
# See LICENSE-MIT in this directory. The rest of the gemifi driver is GPL.
"""State machine for one gemifi device on the script's port (protocol/sysex.md 4.6).

    searching --HELLO ok, caps bit 6--> enumerating --all kinds read--> enabling --debug.events=1--> ready
        |                                                                                          |
        +--HELLO ok, no caps bit 6--> unsupported    BOOTED / keep-alive fails / events turned off +--> searching

A device restart is seen from its BOOTED message (sysex.md 4.6.3). The keep-alive (reading
debug.events back) stays as the fallback: every ~5 s for firmware without BOOTED, every ~30 s
for firmware that has it, in case BOOTED went out before the port was open.

Pure Python, no Live imports. Time is a tick counter: the surface calls tick() from Live's
update_display (about every 100 ms). Requests are stop-and-wait (sysex.md 1): one outstanding
request, resent with the same seq on a timeout, BAD_CHECKSUM or BUSY, up to MAX_TRIES sends.

Layers: a component with `layer` n > 0 (a twin) is live only while mode n - 1 is on. EVENT type 8
says when a mode turns on or off, and the device sends it once for every mode that is on when
debug.events is set to 1, so `layers_on` is known by the time the session is ready. Firmware
without the `layer` byte has no EVENT 8 either: every component is live.
"""

from . import protocol as P

SEARCHING, ENUMERATING, ENABLING, READY, UNSUPPORTED, IDLE = (
    "searching", "enumerating", "enabling", "ready", "unsupported", "idle")

# sysex.md 1: 200 ms reply timeout. A tick is ~100 ms but may come early, so wait 3 ticks.
TIMEOUT_TICKS = 3
MAX_TRIES = 6                 # 5 resends of the same seq
RESCAN_TICKS = 30             # after a failed search, try HELLO again ~3 s later
KEEPALIVE_TICKS = 50          # in ready, read debug.events every ~5 s to notice a replug / reboot
KEEPALIVE_BOOTED_TICKS = 300  # ... every ~30 s when the device sends BOOTED (sysex.md 4.6.3)

# mcu: names used as roles -> the script's role names (hosts/README.md, "Roles").
MCU_ROLE_MAP = {
    "master": "master_volume",
    "bank_left": "bank_prev",
    "bank_right": "bank_next",
    "channel_left": "strip_prev",
    "channel_right": "strip_next",
    "cycle": "loop",
    "click": "metronome",
}
_MCU_PREFIX_MAP = (("fader_", "volume_"), ("vpot_", "pan_"), ("rec_", "arm_"))


def normalise_role(role):
    """Apply the mcu:->role mapping from hosts/README.md. Unknown names pass through unchanged
    (the mapping layer ignores roles it doesn't know)."""
    role = (role or "").strip().lower()
    if role in MCU_ROLE_MAP:
        return MCU_ROLE_MAP[role]
    for old, new in _MCU_PREFIX_MAP:
        if role.startswith(old) and role[len(old):].isdigit():
            return new + role[len(old):]
    return role


class _Request(object):
    __slots__ = ("cmd", "seq", "payload", "on_ok", "on_error", "tries", "waited")

    def __init__(self, cmd, seq, payload, on_ok, on_error):
        self.cmd, self.seq, self.payload = cmd, seq, bytes(payload)
        self.on_ok, self.on_error = on_ok, on_error
        self.tries = 0
        self.waited = 0


class Device(object):
    """One device. `send(bytes)` puts a SysEx message on the wire.

    Callbacks (assign after construction):
      on_ready(device)  - components are known and EVENTs are on; drive feedback now
      on_lost(device)   - the device stopped answering (or rebooted); feedback state is gone
      on_event(event)   - a protocol.Event (button / key / analog / encoder / touch / pad / layer;
                          `layers_on` is already updated for a layer EVENT)
      on_log(text)      - diagnostics for the script's log
    """

    def __init__(self, send, timeout_ticks=TIMEOUT_TICKS, max_tries=MAX_TRIES, rescan_ticks=RESCAN_TICKS,
                 keepalive_ticks=KEEPALIVE_TICKS, keepalive_booted_ticks=KEEPALIVE_BOOTED_TICKS):
        self._send = send
        self.timeout_ticks = timeout_ticks
        self.max_tries = max_tries
        self.rescan_ticks = rescan_ticks
        self.keepalive_ticks = keepalive_ticks
        self.keepalive_booted_ticks = keepalive_booted_ticks
        self.sends_booted = False     # BOOTED seen, or a GET_COMPONENT reply with the added fields
        self.on_ready = None
        self.on_lost = None
        self.on_event = None
        self.on_log = None
        self.state = IDLE
        self.hello = None
        self.components = {}          # kind -> list of Component, by index
        self.events_before = None     # debug.events before we turned it on (restored on release)
        self.layers_on = set()        # mode indexes whose layers are on (EVENT type 8)
        self._seq = 0
        self._pending = None
        self._queue = []
        self._wait = 0                # ticks until the next scan / keep-alive
        self._enum_kind = 0
        self._sysex_buf = None

    # ---- public ----

    def start(self):
        """(Re)start from scratch: forget everything and send HELLO."""
        self._pending = None
        self._queue = []
        self.hello = None
        self.components = {}
        self.layers_on = set()
        self.sends_booted = False
        self.state = SEARCHING
        self._request(P.HELLO, b"", self._on_hello, self._on_search_failed)

    def stop(self):
        self._pending = None
        self._queue = []
        self.state = IDLE

    @property
    def ready(self):
        return self.state == READY

    @property
    def feedback_ok(self):
        """sysex.md 4.6.2: notifications only go to a device that set caps bit 6."""
        return self.hello is not None and self.hello.has_feedback

    def is_live(self, component):
        """Whether `component` takes its physical control now: not a twin (layer 0 or not
        reported), or a twin whose mode is on."""
        return component.live(self.layers_on)

    def all_components(self):
        out = []
        for kind in P.ALL_KINDS:
            out.extend(self.components.get(kind, []))
        return out

    def notify(self, cmd, payload):
        """Send an FB_* notification. Never answered, never retried, ignores stop-and-wait."""
        if not P.NOTIFY_FIRST <= cmd <= P.NOTIFY_LAST:
            raise ValueError("not a notification command: 0x%02X" % cmd)
        if not self.feedback_ok:
            return False
        self._send(P.build_frame(cmd, 0, payload))
        return True

    def release(self):
        """Hand everything back to the device (sysex.md 4.6 step 5): FB_RELEASE 0 and debug.events
        back to what it was. Fire-and-forget: this runs while the script is going away."""
        if not self.feedback_ok:
            return
        self.notify(P.FB_RELEASE, P.fb_release(P.RELEASE_ALL))
        if self.events_before is not None and self.events_before != 1 and self.state == READY:
            self._send(P.build_frame(P.SET_PARAM, self._next_seq(),
                                     P.req_set_param(P.PARAM_DEBUG_EVENTS, 0, self.events_before)))
        self.stop()

    def tick(self):
        """Call about every 100 ms."""
        p = self._pending
        if p is not None:
            p.waited += 1
            if p.waited >= self.timeout_ticks:
                self._resend_or_fail(p, "timeout")
            return
        if self._wait > 0:
            self._wait -= 1
            if self._wait == 0:
                if self.state == SEARCHING:
                    self.start()
                elif self.state == READY:
                    self._keepalive()

    def receive(self, midi_bytes):
        """Feed every incoming SysEx message (or fragment) here. Returns True if it was ours."""
        msg = self._reassemble(midi_bytes)
        if msg is None or not P.is_gemifi(msg):
            return False
        try:
            frame = P.parse_frame(msg)
        except P.ProtocolError as e:
            self._log("dropped bad frame: %s" % e)
            return True
        if frame.is_unsolicited:
            self._on_unsolicited(frame)
        elif frame.is_reply:
            self._on_reply(frame)
        return True

    # ---- requests ----

    def _next_seq(self):
        self._seq = (self._seq + 1) & 0x7F
        return self._seq

    def _request(self, cmd, payload, on_ok, on_error=None):
        r = _Request(cmd, 0, payload, on_ok, on_error)
        if self._pending is None:
            self._start(r)
        else:
            self._queue.append(r)

    def _start(self, r):
        r.seq = self._next_seq()
        self._pending = r
        self._transmit(r)

    def _transmit(self, r):
        r.tries += 1
        r.waited = 0
        self._send(P.build_frame(r.cmd, r.seq, r.payload))

    def _resend_or_fail(self, r, why):
        if r.tries < self.max_tries:
            self._transmit(r)
            return
        self._log("0x%02X failed after %d tries (%s)" % (r.cmd, r.tries, why))
        self._finish(r, None)

    def _finish(self, r, frame):
        """Complete `r` with `frame` (None = gave up), then start the next queued request."""
        self._pending = None
        if frame is not None and frame.status == P.OK:
            r.on_ok(frame)
        elif r.on_error is not None:
            r.on_error(frame.status if frame is not None else None)
        if self._pending is None and self._queue:
            self._start(self._queue.pop(0))

    def _on_reply(self, frame):
        r = self._pending
        if r is None or frame.cmd != (r.cmd | P.REPLY) or frame.seq != r.seq or not frame.payload:
            return                                    # stale or stray reply
        status = frame.status
        if status in (P.BAD_CHECKSUM, P.BUSY):
            self._resend_or_fail(r, P.status_name(status))
            return
        self._finish(r, frame)

    # ---- unsolicited ----

    def _on_unsolicited(self, frame):
        if frame.cmd == P.EVENT:
            try:
                ev = P.parse_event(frame.payload)
            except P.ProtocolError:
                return
            if ev.type == P.EVENT_LAYER and self.state in (ENABLING, READY):
                # Kept before ready too: the replay after SET_PARAM debug.events may come
                # ahead of its reply.
                if ev.value:
                    self.layers_on.add(ev.index)
                else:
                    self.layers_on.discard(ev.index)
            if self.state == READY and self.on_event is not None:
                self.on_event(ev)
        elif frame.cmd == P.LOG:
            text = bytes(frame.payload[5:]).decode("ascii", "replace")
            self._log("device: %s" % text)
        elif frame.cmd == P.PARAM_CHANGED:
            try:
                pid, _index, value = P.parse_param_reply(frame.payload)
            except P.ProtocolError:
                return
            if pid == P.PARAM_DEBUG_EVENTS and value == 0 and self.state == READY:
                self._log("debug.events turned off on the device; turning it back on")
                self._enable_events()
        elif frame.cmd == P.BOOTED:
            self._on_booted(frame)
        # PRESET_CHANGED, RAW (another host's MONITOR) and UNLOCKED (another host flashing it):
        # nothing for a DAW script to do.

    def _on_booted(self, frame):
        """sysex.md 4.6.3: the device restarted, so every FB_* state and debug.events are gone.
        Start again from HELLO (an upload may have changed its components) and re-send everything."""
        try:
            h = P.parse_booted(frame.payload)
        except P.ProtocolError as e:
            self._log("bad BOOTED: %s" % e)
            return
        if self.state == IDLE:
            return                                # not started (or released): nothing to re-send
        self._lost("device restarted (BOOTED, fw %s)" % h.version_text)
        self.sends_booted = True                  # after _lost: start() forgets the old device

    # ---- the steps ----

    def _on_hello(self, frame):
        try:
            self.hello = P.parse_hello(frame.payload)
        except P.ProtocolError as e:
            self._log("bad HELLO reply: %s" % e)
            self._on_search_failed(None)
            return
        h = self.hello
        if h.role != 0:
            # The bootloader (an update in progress): look again later, the app comes back.
            self._log("device is in its bootloader; waiting for the app")
            self.hello = None
            self._on_search_failed(None)
            return
        if not h.has_feedback:
            self.state = UNSUPPORTED
            self._log("device %s (fw %s, role %d) has no host feedback (caps 0x%X)"
                      % (h.node or "?", h.version_text, h.role, h.caps))
            return
        self._log("found %s fw %s" % (h.node or "gemifi device", h.version_text))
        self.state = ENUMERATING
        self.components = {}
        self._enum_kind = 0
        self._enum_next_kind()

    def _on_search_failed(self, status):
        self.state = SEARCHING
        self._wait = self.rescan_ticks

    def _enum_next_kind(self):
        while self._enum_kind < len(P.ALL_KINDS):
            kind = P.ALL_KINDS[self._enum_kind]
            self._enum_kind += 1
            self.components[kind] = []
            self._get_component(kind, 0)
            return
        self._enum_done()

    def _get_component(self, kind, index):
        def ok(frame):
            try:
                c = P.parse_component_reply(frame.payload)
            except P.ProtocolError as e:
                self._log("bad GET_COMPONENT reply (%d, %d): %s" % (kind, index, e))
                self._enum_next_kind()
                return
            c.kind, c.index = kind, index         # trust our request over the echo
            c.role = normalise_role(c.role)
            if c.reports_added_fields:
                self.sends_booted = True          # same firmware change as BOOTED
            self.components[kind].append(c)
            if index + 1 < c.count:
                self._get_component(kind, index + 1)
            else:
                self._enum_next_kind()

        def err(status):
            if status == P.BAD_PARAM:             # none of this kind (index 0) or past the end
                self._enum_next_kind()
            elif status is None:
                self._lost("GET_COMPONENT timed out")
            else:
                self._log("GET_COMPONENT (%d, %d) -> %s; skipping the kind" % (kind, index, P.status_name(status)))
                self._enum_next_kind()

        self._request(P.GET_COMPONENT, P.req_get_component(kind, index), ok, err)

    def _enum_done(self):
        self.state = ENABLING
        n = sum(len(v) for v in self.components.values())
        self._log("%d components" % n)

        def got(frame):
            _pid, _index, value = P.parse_param_reply(frame.payload)
            if self.events_before is None:
                self.events_before = value
            self._enable_events()

        def err(status):
            if status is None:
                self._lost("GET_PARAM debug.events timed out")
            else:
                self._enable_events()

        self._request(P.GET_PARAM, P.req_get_param(P.PARAM_DEBUG_EVENTS, 0), got, err)

    def _enable_events(self):
        def ok(frame):
            _pid, _index, value = P.parse_param_reply(frame.payload)
            if value != 1:
                self._log("debug.events is %d after SET_PARAM 1" % value)
            was_ready = self.state == READY
            self.state = READY
            self._wait = self._keepalive_interval()
            if not was_ready and self.on_ready is not None:
                self.on_ready(self)

        def err(status):
            if status is None:
                self._lost("SET_PARAM debug.events timed out")
            else:
                self._log("SET_PARAM debug.events -> %s" % P.status_name(status))
                self.state = UNSUPPORTED

        self._request(P.SET_PARAM, P.req_set_param(P.PARAM_DEBUG_EVENTS, 0, 1), ok, err)

    def _keepalive(self):
        def ok(frame):
            _pid, _index, value = P.parse_param_reply(frame.payload)
            if value != 1:
                # Rebooted or replugged: its FB_* states are gone and the firmware may differ.
                self._lost("debug.events is off (device restarted?)")
                return
            self._wait = self._keepalive_interval()

        def err(status):
            if status is None:
                self._lost("keep-alive timed out")
            else:
                self._wait = self._keepalive_interval()

        self._request(P.GET_PARAM, P.req_get_param(P.PARAM_DEBUG_EVENTS, 0), ok, err)

    def _keepalive_interval(self):
        return self.keepalive_booted_ticks if self.sends_booted else self.keepalive_ticks

    def _lost(self, why):
        self._log("lost device: %s" % why)
        was_ready = self.state == READY
        self.state = SEARCHING
        self._pending = None
        self._queue = []
        if was_ready and self.on_lost is not None:
            self.on_lost(self)
        self.start()

    # ---- helpers ----

    def _reassemble(self, midi_bytes):
        data = tuple(midi_bytes)
        if not data:
            return None
        if data[0] == 0xF0:
            if data[-1] == 0xF7:
                self._sysex_buf = None
                return data
            self._sysex_buf = list(data)
            return None
        if self._sysex_buf is not None:
            self._sysex_buf.extend(data)
            if data[-1] == 0xF7:
                msg, self._sysex_buf = tuple(self._sysex_buf), None
                return msg
            if len(self._sysex_buf) > 4096:
                self._sysex_buf = None
        return None

    def _log(self, text):
        if self.on_log is not None:
            self.on_log(text)
