#!/usr/bin/python3
# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Control Device Driver
#
# Zynthian Control Device Driver for gemifi controllers (host feedback)
#
# Copyright (C) 2026 Benjamin Van Treese <vantreeseba@gmail.com>
#
# ******************************************************************************
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of
# the License, or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# For a full copy of the GNU General Public License see the LICENSE.txt file.
#
# ******************************************************************************

"""gemifi devices: MIDI controllers that describe their own controls over SysEx.

The driver asks the device which buttons, faders, encoders and LEDs it has and what each one
is for (its "role": play, volume_3, mute_1, device_param_2, clip_1_1...), hears presses and
moves as EVENT messages, and drives the LEDs, fader pick-up, encoder rings and display from
Zynthian's state. Pads with clip_T_S roles are a window on zynpad's launcher grid (chains x
phrases), launched the way zynpad launches pads. The device's notes and CCs keep reaching the
chains as usual.

Specs: gemifi protocol/daw-scripts.md and hosts/zynthian/README.md. The protocol, session
and role logic in gemifi/ are the MIT reference code from gemifi's Ableton Live script;
gemifi/zynthian_model.py adapts Zynthian to it.
"""

import logging
import threading

from zyncoder.zyncore import lib_zyncore
from zyngine.ctrldev.zynthian_ctrldev_base import zynthian_ctrldev_zynpad, zynthian_ctrldev_zynmixer
from zyngui import zynthian_gui_config
from zyngine.ctrldev.gemifi import protocol as P
from zyngine.ctrldev.gemifi.device import Device
from zyngine.ctrldev.gemifi.zynthian_model import ZynSong, ZynMapping

TICK_SECONDS = 0.1      # the session's clock: request timeouts, retries, keep-alive, feedback


def _switch_thresholds():
	"""Zynthian's bold / long press times for its switches, in µs (read each time: the config
	module sets them from the environment)."""
	return zynthian_gui_config.zynswitch_bold_us, zynthian_gui_config.zynswitch_long_us


class zynthian_ctrldev_gemifi(zynthian_ctrldev_zynpad, zynthian_ctrldev_zynmixer):

	# Any port: the user picks "gemifi" for a device. HELLO confirms it's a gemifi device with
	# host feedback; on anything else the driver stays idle and sends nothing but HELLO.
	dev_ids = ["*"]
	driver_description = "gemifi controllers (host feedback)"
	# The keys and knobs must keep playing the chains.
	unroute_from_chains = False
	# True: play / record / stop drive the MIDI (SMF) player and recorder instead of audio.
	midi_transport = False
	# False: faders jump to where they are as soon as they move. True: they pick up, doing
	# nothing until they reach the current value.
	fader_pickup = False

	def __init__(self, state_manager, idev_in, idev_out=None):
		super().__init__(state_manager, idev_in, idev_out)
		self.cols = 8
		self._lock = threading.RLock()
		self._stop = threading.Event()
		self._thread = None
		self._sleeping = False
		self._logged_roles = set()
		self.song = ZynSong(state_manager, lambda: self.chain_ids_filtered, self.midi_transport,
							pad_press=self.on_pad_press, pad_release=self.on_pad_release,
							switch_thresholds=_switch_thresholds)
		self.device = Device(self._send)
		self.mapping = ZynMapping(self.song, self.device.notify, log=self._log)
		self.device.on_ready = self._on_ready
		self.device.on_lost = self._on_lost
		self.device.on_event = self.mapping.handle_event
		self.device.on_log = self._log

	# ---- lifecycle ----

	def init(self):
		super().init()
		with self._lock:
			self.device.start()
		self._stop.clear()
		self._thread = threading.Thread(target=self._run, name=f"gemifi-{self.idev}", daemon=True)
		self._thread.start()

	def end(self):
		self._stop.set()
		if self._thread is not None and self._thread is not threading.current_thread():
			self._thread.join(1.0)
		self._thread = None
		with self._lock:
			# FB_RELEASE 0 and debug.events back to what it was (daw-scripts.md §3 step 6)
			self.device.release()
			self.mapping.release()
		super().end()

	def _run(self):
		while not self._stop.wait(TICK_SECONDS):
			self.tick()

	def tick(self):
		"""Every TICK_SECONDS: retries and keep-alive, then feedback for what changed."""
		with self._lock:
			try:
				self.device.tick()
				if self.device.ready and not self._sleeping:
					self.mapping.tick()
					self.scroll_h = self.mapping.bank_offset
			except Exception as e:
				logging.exception(f"gemifi: error in tick => {e}")

	# ---- MIDI ----

	def _send(self, msg):
		if self.idev_out is None:
			return
		msg = bytes(msg)
		lib_zyncore.dev_send_midi_event(self.idev_out, msg, len(msg))

	def midi_event(self, ev):
		# Only gemifi SysEx is ours: notes and CCs go on to the UI (MIDI learn...) as usual.
		if len(ev) and ev[0] == 0xF0 and P.is_gemifi(ev):
			with self._lock:
				try:
					self.device.receive(ev)
				except Exception as e:
					logging.exception(f"gemifi: error handling SysEx => {e}")
			return True
		return False

	# ---- session callbacks (called with the lock held) ----

	def _log(self, text):
		logging.info(f"gemifi [{self.idev}]: {text}")

	def _on_ready(self, device):
		self.mapping.fader_pickup = self.fader_pickup
		if not self.fader_pickup:
			# Drop pick-up targets left from earlier, so takeover: pickup faders send at once.
			device.notify(P.FB_RELEASE, P.fb_release(P.RELEASE_VALUES))
		self.mapping.bind(device.all_components())
		for kind, index, role in self.mapping.unknown_roles:
			if role not in self._logged_roles:
				self._logged_roles.add(role)
				self._log(f"ignoring role '{role}' ({P.KIND_NAMES.get(kind, kind)} {index}): not supported in Zynthian")
		h = device.hello
		self._log(f"connected to {h.node or 'device'} (fw {h.version_text})")
		if not self._sleeping:
			self.mapping.refresh()

	def _on_lost(self, device):
		self.mapping.release()

	# ---- Zynthian -> device ----

	def refresh(self):
		"""Chains changed, a snapshot loaded, zynseq's state reloaded...: re-send everything."""
		# Not zynpad's refresh(): it repaints pads through light_off() and update_pad(). The
		# mapping draws the clip grid itself.
		zynthian_ctrldev_zynmixer.refresh(self)
		with self._lock:
			if self.device.ready and not self._sleeping:
				self.mapping.forget()
				self.mapping.refresh()

	def update_mixer_strip(self, chan, symbol, value, mixbus=False):
		# The tick polls everything anyway; this just makes mixer changes show at once.
		with self._lock:
			if self.device.ready and not self._sleeping:
				self.mapping.refresh()

	def update_seq_state(self, phrase=None, chan=None):
		"""zynpad: a sequence's play state changed (SS_SEQ_PLAY_STATE). The mapping polls the grid
		every tick anyway; this shows the change at once."""
		self.update_mixer_strip(None, None, None)

	def update_pad(self, row, col, pad_info):
		pass	# the mapping draws the grid (see update_seq_state)

	def pad_off(self, col, row):
		pass	# no punch-in countdown flash on gemifi pads

	def on_active_chain(self, active_chain_id):
		super().on_active_chain(active_chain_id)
		self.update_mixer_strip(None, None, None)

	def light_off(self):
		with self._lock:
			if self.device.ready:
				self.mapping.all_leds_off(self.device.all_components())
				self.mapping.blank_displays()

	def sleep_on(self):
		with self._lock:
			self._sleeping = True
		self.light_off()

	def sleep_off(self):
		with self._lock:
			self._sleeping = False
		self.refresh()

# ------------------------------------------------------------------------------
