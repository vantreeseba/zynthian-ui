#!/usr/bin/python3
# -*- coding: utf-8 -*-
# Unit tests for zynclap
# Tests use two letters to define order of groups and two digit integer to define order within group
# They need a running JACK server and the test plugin built by build.sh

import os
import json
import base64
import struct
import tempfile
import unittest
import jack
from time import sleep
from queue import Queue, Empty
from threading import Thread
from subprocess import Popen, run, PIPE

build_dir = os.path.dirname(os.path.abspath(__file__)) + "/build"
zynclap_bin = build_dir + "/zynclap"
plugin_path = build_dir + "/test/zyntest.clap"
plugin_id = "org.zynthian.zyntest"

PARAM_GAIN = 1
PARAM_MODE = 2
PARAM_LAST_NOTE = 3

client = jack.Client("zynclap_test")
audio_in = client.inports.register("audio_in")
audio_out = client.outports.register("audio_out")
midi_out = client.midi_outports.register("midi_out")
send_level = 0.0
send_midi = None
last_peak = 0.0


@client.set_process_callback
def process(frames):
    global send_midi
    global last_peak
    audio_out.get_array().fill(send_level)
    last_peak = float(abs(audio_in.get_array()).max())
    midi_out.clear_buffer()
    if send_midi:
        midi_out.write_midi_event(0, send_midi)
        send_midi = None


client.activate()


class TestZynClap(unittest.TestCase):
    @classmethod
    def setUpClass(self):
        self.proc = Popen([zynclap_bin, "-n", "zynclap_dut", plugin_path, plugin_id],
                          text=True, bufsize=1, stdin=PIPE, stdout=PIPE)
        self.lines = Queue()
        self.reader = Thread(target=self.read_lines, daemon=True)
        self.reader.start()
        self.startup = self.wait_prompt(self)

    @classmethod
    def tearDownClass(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)

    @classmethod
    def read_lines(self):
        for line in self.proc.stdout:
            self.lines.put(line.strip())

    # Get the lines sent by the host until the next prompt
    def wait_prompt(self):
        res = []
        while True:
            line = self.lines.get(timeout=5)
            if line == ">":
                return res
            res.append(line)

    # Send a command and get the lines sent by the host until its prompt
    def cmd(self, command):
        self.proc.stdin.write(command + "\n")
        return self.wait_prompt()

    # Wait for a line sent by the host out of a command, like parameter feedback
    def wait_line(self, expected, timeout=2):
        try:
            while True:
                if self.lines.get(timeout=timeout) == expected:
                    return True
        except Empty:
            return False

    # Get the plugin's state as (gain, mode)
    def get_state(self):
        res = self.cmd("state")
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0].startswith("#STA> "))
        return struct.unpack("<dd", base64.b64decode(res[0][6:]))

    # Wait until the plugin's state has the given value, as parameters are set by the audio thread
    def wait_state(self, expected):
        for i in range(50):
            if self.get_state() == expected:
                return True
            sleep(0.02)
        return False

    # Wait until the level received from the plugin has the given value
    def wait_level(self, expected):
        for i in range(100):
            if abs(last_peak - expected) < 0.001:
                return True
            sleep(0.02)
        return False

    # Startup tests

    def test_aa00_jackname(self):
        self.assertEqual(self.startup[0], "JACK Name: zynclap_dut")

    def test_aa01_params(self):
        params = [json.loads(line[6:]) for line in self.startup if line.startswith("#PRM> ")]
        self.assertEqual([p["id"] for p in params], [PARAM_GAIN, PARAM_MODE, PARAM_LAST_NOTE])
        gain, mode, last_note = params
        self.assertEqual((gain["name"], gain["module"]), ("Gain", "Main"))
        self.assertEqual((gain["min"], gain["max"], gain["default"], gain["value"]), (0, 2, 1, 1))
        self.assertFalse(gain["stepped"])
        self.assertEqual(gain["labels"], [])
        self.assertTrue(mode["stepped"] and mode["enum"])
        self.assertEqual(mode["labels"], ["Normal", "Mute", "Invert"])
        self.assertTrue(last_note["readonly"])

    def test_aa02_ports(self):
        ports = [port.name for port in client.get_ports("zynclap_dut:")]
        self.assertEqual(sorted(ports), ["zynclap_dut:in_1", "zynclap_dut:in_2", "zynclap_dut:midi_in",
                                         "zynclap_dut:out_1", "zynclap_dut:out_2"])

    # Parameter tests

    def test_ab00_set_param(self):
        self.assertEqual(self.cmd(f"set {PARAM_GAIN} 0.5"), [])
        self.assertTrue(self.wait_state((0.5, 0.0)))
        self.cmd(f"set {PARAM_MODE} 2")
        self.assertTrue(self.wait_state((0.5, 2.0)))

    def test_ab01_resend_params(self):
        params = [json.loads(line[6:]) for line in self.cmd("params")]
        self.assertEqual([p["value"] for p in params[:2]], [0.5, 2])

    def test_ab02_unknown_param(self):
        self.assertEqual(self.cmd("set 999 1"), [])
        self.assertEqual(self.get_state(), (0.5, 2.0))

    def test_ab03_unknown_command(self):
        self.assertEqual(self.cmd("nonsense"), [])

    # State tests

    def test_ac00_load_state(self):
        saved = self.cmd("state")[0][6:]
        self.cmd(f"set {PARAM_GAIN} 2")
        self.cmd(f"set {PARAM_MODE} 0")
        self.assertTrue(self.wait_state((2.0, 0.0)))
        # Loading reports the values that changed
        res = self.cmd(f"load {saved}")
        self.assertEqual(sorted(res), [f"#CTR> {PARAM_GAIN}=0.5", f"#CTR> {PARAM_MODE}=2"])
        self.assertEqual(self.get_state(), (0.5, 2.0))

    def test_ac01_load_default_state(self):
        default = base64.b64encode(struct.pack("<dd", 1.0, 0.0)).decode()
        self.cmd(f"load {default}")
        self.assertEqual(self.get_state(), (1.0, 0.0))

    # Audio & MIDI tests

    def test_ad00_audio(self):
        global send_level
        client.connect(audio_out, "zynclap_dut:in_1")
        client.connect("zynclap_dut:out_1", audio_in)
        send_level = 0.5
        self.assertTrue(self.wait_level(0.5))
        self.cmd(f"set {PARAM_GAIN} 0.5")
        self.assertTrue(self.wait_level(0.25))
        self.cmd(f"set {PARAM_MODE} 1")
        self.assertTrue(self.wait_level(0.0))

    def test_ad01_bypass(self):
        # Plugin is muted by the previous test
        self.cmd("bypass 1")
        self.assertTrue(self.wait_level(0.5))
        self.cmd("bypass 0")
        self.assertTrue(self.wait_level(0.0))

    def test_ad02_midi_feedback(self):
        global send_midi
        client.connect(midi_out, "zynclap_dut:midi_in")
        sleep(0.1)
        send_midi = (0x90, 64, 100)
        self.assertTrue(self.wait_line(f"#CTR> {PARAM_LAST_NOTE}=64"))

    # Scan tests

    def scan(self, clap_path):
        env = os.environ.copy()
        env["CLAP_PATH"] = clap_path
        env["HOME"] = "/nonexistent"
        res = run([zynclap_bin, "--scan"], env=env, stdout=PIPE, text=True, timeout=60)
        self.assertEqual(res.returncode, 0)
        return json.loads(res.stdout)

    def test_ae00_scan(self):
        plugins = [p for p in self.scan(build_dir + "/test") if p["id"] == plugin_id]
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0]["name"], "ZynTest")
        self.assertEqual(plugins[0]["path"], plugin_path)
        self.assertEqual(plugins[0]["features"], ["audio-effect", "utility", "stereo"])

    def test_ae01_scan_broken_file(self):
        with tempfile.TemporaryDirectory() as dpath:
            with open(dpath + "/broken.clap", "w") as f:
                f.write("This is not a plugin")
            plugins = self.scan(f"{dpath}:{build_dir}/test")
            self.assertEqual([p["id"] for p in plugins if p["path"].startswith(build_dir)], [plugin_id])
            self.assertEqual([p for p in plugins if p["path"].startswith(dpath)], [])

    def test_ae02_scan_missing_dir(self):
        self.assertEqual([p for p in self.scan("/nonexistent/clap") if p["path"].startswith("/nonexistent")], [])


unittest.main()
