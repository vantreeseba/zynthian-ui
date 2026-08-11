# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Engine (zynthian_engine_tempo)
#
# zynthian_engine implementation for Tempo control
#
# Copyright (C) 2015-2026 Fernando Moyano <jofemodo@zynthian.org>
#
# ******************************************************************************
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of
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

import logging
from time import monotonic
from collections import deque

import zynconf
from zyncoder.zyncore import lib_zyncore
from zyngine.zynthian_engine import zynthian_engine
from zyngine.zynthian_controller import zynthian_controller
from zyngui import zynthian_gui_config
import zynautoconnect

# ------------------------------------------------------------------------------
# Tempo Engine Class
# ------------------------------------------------------------------------------


class zynthian_engine_tempo(zynthian_engine):

    # ---------------------------------------------------------------------------
    # Controllers & Screens
    # ---------------------------------------------------------------------------

    _ctrl_screens = [
        ["Tempo", ["bpm", "metro_enable", "metro_volume", "metro_out"]]
    ]

    # ----------------------------------------------------------------------------
    # ZynAPI variables
    # ----------------------------------------------------------------------------

    zynapi_instance = None

    # ----------------------------------------------------------------------------
    # Initialization
    # ----------------------------------------------------------------------------

    def __init__(self, state_manager=None, proc=None):
        super().__init__(state_manager)

        self.type = "Tempo"
        self.name = "Tempo"
        self.nickname = "TP"
        self.custom_gui_fpath = "/zynthian/zynthian-ui/zyngui/zynthian_widget_tempo.py"
        self.processor = proc

        self.audio_out = []
        self.options['midi_chan'] = False
        self.options['replace'] = False

        self.zctrls = None
        self.zctrl_metro_out = None

    # ---------------------------------------------------------------------------
    # Processor Management
    # ---------------------------------------------------------------------------

    def get_path(self, processor):
        return self.name

    # ---------------------------------------------------------------------------
    # MIDI Channel Management
    # ---------------------------------------------------------------------------

    # ----------------------------------------------------------------------------
    # Bank Managament
    # ----------------------------------------------------------------------------

    def get_bank_list(self, processor=None):
        return [("", None, "", None)]

    def set_bank(self, processor, bank):
        return True

    # ----------------------------------------------------------------------------
    # Preset Managament
    # ----------------------------------------------------------------------------

    def get_preset_list(self, bank, processor=None):
        return [("", None, "", None)]

    def set_preset(self, processor, preset, preload=False):
        return True

    def cmp_presets(self, preset1, preset2):
        return True

    # ----------------------------------------------------------------------------
    # Controllers Managament
    # ----------------------------------------------------------------------------

    def get_metro_out_zctrl(self):
        """Build (once) the metronome output selector: Main mixbus or a hardware output"""

        if self.zctrl_metro_out is None:
            labels = ["Main"]
            try:
                port_count = len(zynautoconnect.get_hw_audio_dst_ports())
            except Exception:
                port_count = 0
            for i in range(0, port_count, 2):
                labels.append(f"{i+1}")
                labels.append(f"{i+2}")
                labels.append(f"{i+1}+{i+2}")
            if zynthian_gui_config.metronome_output in labels:
                value = zynthian_gui_config.metronome_output
            else:
                value = "Main"
            self.zctrl_metro_out = zynthian_controller(self, 'metro_out', {
                'name': 'Metronome Output',
                'labels': labels,
                'value': value
            })
        return self.zctrl_metro_out

    def get_controllers_dict(self, processor=None, ctrl_list=None):
        if zynautoconnect.get_ext_clock_zmip() < 0:
            self._ctrl_screens = [["Tempo", ["bpm", "metro_enable", "metro_volume", "metro_out"]]]
        else:
            self._ctrl_screens = [["Tempo", ["ppqn", "metro_enable", "metro_volume", "metro_out"]]]

        if processor:
            if not processor.controllers_dict:
                processor.controllers_dict = {
                    "bpm": self.state_manager.zynseq.zctrl_tempo,
                    "metro_enable": self.state_manager.zynseq.zctrl_metro_mode,
                    "metro_volume": self.state_manager.zynseq.zctrl_metro_volume,
                    "ppqn": self.state_manager.zynseq.zctrl_ppqn,
                    "metro_out": self.get_metro_out_zctrl()
                }
                # The shared zynseq zctrls are created without a processor: bind them here
                # so MIDI-learn bindings can be saved/restored ([processor.id, symbol])
                for zctrl in processor.controllers_dict.values():
                    zctrl.processor = processor
            return processor.controllers_dict
        return  {
            "bpm": self.state_manager.zynseq.zctrl_tempo,
            "metro_enable": self.state_manager.zynseq.zctrl_metro_mode,
            "metro_volume": self.state_manager.zynseq.zctrl_metro_volume,
            "ppqn": self.state_manager.zynseq.zctrl_ppqn,
            "metro_out": self.get_metro_out_zctrl()
        }

    def send_controller_value(self, zctrl):
        if zctrl.symbol == "metro_out":
            label = zctrl.get_value2label()
            if label != zynthian_gui_config.metronome_output:
                zynthian_gui_config.metronome_output = label
                zynconf.save_config({
                    "ZYNTHIAN_METRONOME_OUTPUT": label
                })
                zynautoconnect.request_audio_connect(True)

    # ----------------------------------------------------------------------------
    # Special
    # ----------------------------------------------------------------------------


# ******************************************************************************
