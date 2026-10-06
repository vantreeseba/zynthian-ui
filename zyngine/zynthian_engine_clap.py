# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Engine (zynthian_engine_clap)
#
# zynthian_engine implementation for CLAP plugins, hosted by zynclap
#
# Copyright (C) 2026 Zynthian Project
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

import json
import logging
from threading import Event

import zyngine.zynthian_lv2 as zynthian_lv2
import zyngine.zynthian_clap as zynthian_clap
from zyngine.zynthian_engine import zynthian_engine
from zyngine.zynthian_engine_jalv import zynthian_engine_jalv_base
from zyngine.zynthian_controller import zynthian_controller

# ------------------------------------------------------------------------------
# CLAP Engine Class => Engine for CLAP plugins
# ------------------------------------------------------------------------------


class zynthian_engine_clap(zynthian_engine_jalv_base):

    # Max time waiting for the plugin state, in seconds
    STATE_TIMEOUT = 2.0

    # ----------------------------------------------------------------------------
    # Initialization
    # ----------------------------------------------------------------------------

    def __init__(self, eng_code, state_manager, dryrun=False, jackname=None):
        zynthian_engine.__init__(self, state_manager)

        self.proc_poll_thread = None

        if state_manager:
            self.eng_info = self.chain_manager.engine_info[eng_code]
        else:
            self.eng_info = zynthian_lv2.get_engines()[eng_code]

        self.type = self.eng_info["TYPE"]
        self.name = "CLAP/" + self.eng_info["NAME"]
        self.nickname = eng_code
        self.plugin_name = self.eng_info["NAME"]
        self.plugin_url = self.eng_info['URL']
        self.plugin_path = self.eng_info['PATH']

        self.bypass_zctrl = None
        self.native_gui = False

        self.clap_zctrl_dict = {}
        self.state_reply = None
        self.state_event = Event()

        # Setup MIDI Controllers
        self._ctrls = []
        self._ctrl_screens = []

        if not dryrun:
            if jackname:
                self.jackname = jackname
            else:
                self.jackname = self.chain_manager.get_next_jackname(self.plugin_name)

            logging.debug("CREATING CLAP ENGINE => {}".format(self.jackname))

            self.command = [zynthian_clap.ZYNCLAP_BIN, "-n", self.jackname, self.plugin_path, self.plugin_url]
            self.command_prompt = ">"

            # Instance the host with the plugin. Parameters are only known once the plugin is running.
            params = []
            output = self.start()
            if output:
                for line in output.split("\n"):
                    if line[0:10] == "JACK Name:":
                        self.jackname = line[11:].strip()
                        logging.debug("Jack Name => {}".format(self.jackname))
                    elif line[0:5] == "#PRM>":
                        try:
                            params.append(json.loads(line[6:]))
                        except Exception as e:
                            logging.warning(f"Wrong parameter info when parsing zynclap output => {line} ({e})")

            # Generate CLAP-Plugin Controllers
            self.clap_zctrl_dict = self.get_clap_controllers_dict(params)
            self.generate_ctrl_screens(self.clap_zctrl_dict)

        self.reset()

    # ---------------------------------------------------------------------------
    # Subprocess Management & IPC
    # ---------------------------------------------------------------------------

    def proc_get_output(self):
        lines = []
        while not self.proc_exit:
            line = self.proc.stdout.readline()
            if not line:
                logging.error(f"CLAP host for '{self.plugin_name}' ended unexpectedly: {' | '.join(lines)}")
                break
            line = line.strip()
            if line == self.command_prompt:
                break
            elif line:
                lines.append(line)
        return "\n".join(lines)

    def proc_poll_thread_task(self):
        while self.proc and not self.proc_exit:
            line = self.proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line:
                self.proc_poll_parse_line(line)

    def proc_poll_parse_line(self, line):
        match line[0:5]:
            case "#CTR>":
                self.proc_parse_ctrl_value(line[6:])
            case "#STA>":
                self.state_reply = line[6:].strip()
                self.state_event.set()
            case _:
                if line == self.command_prompt:
                    pass
                elif line:
                    logging.debug(f"LOG {self.jackname} > " + line)

    def proc_parse_ctrl_value(self, line):
        parts = line.split("=")
        if len(parts) != 2:
            logging.warning(f"Wrong controller format when parsing zynclap output => {line}")
            return
        try:
            val = float(parts[1])
        except Exception as e:
            logging.warning(f"Wrong controller value when parsing zynclap output => {line}")
            return
        try:
            zctrl = self.clap_zctrl_dict[parts[0]]
        except KeyError:
            # Hidden & read-only parameters have no controller
            return
        if not zctrl.get_ignore_engine_fb():
            zctrl.set_value(val, False)

    # ----------------------------------------------------------------------------
    # Bank Managament
    # ----------------------------------------------------------------------------

    def get_bank_list(self, processor=None):
        return [("", None, "None", None)]

    def set_bank(self, processor, bank):
        return True

    # ----------------------------------------------------------------------------
    # Preset Managament
    # ----------------------------------------------------------------------------

    def get_preset_list(self, bank, processor=None):
        return [("", None, "", None)]

    def set_preset(self, processor, preset, preload=False):
        # TODO: Presets are not implemented yet
        return

    # ----------------------------------------------------------------------------
    # Controllers Managament
    # ----------------------------------------------------------------------------

    def get_clap_controllers_dict(self, params):
        logging.info("Getting Controller List from CLAP Plugin ...")
        zctrls = {}
        self.bypass_zctrl = None
        for i, info in enumerate(params):
            try:
                if info['hidden'] or info['readonly']:
                    continue

                # CLAP parameter IDs are stable, so they are used as symbols
                symbol = str(info['id'])
                if info['module']:
                    group_symbol = info['module']
                    group_name = info['module'].split("/")[-1]
                else:
                    group_symbol = None
                    group_name = None

                options = {
                    'name': info['name'],
                    'group_symbol': group_symbol,
                    'group_name': group_name,
                    'graph_path': info['id'],
                    'is_toggle': False,
                    'is_integer': info['stepped'],
                    'is_bypass': info['bypass'],
                    'bypass_value': 1,
                    # Keep the plugin's parameter order
                    'display_priority': 100000 - i
                }

                if info['stepped']:
                    value_min = int(info['min'])
                    value_max = int(info['max'])
                    options['value'] = int(info['value'])
                    options['value_default'] = int(info['default'])
                    options['value_min'] = value_min
                    options['value_max'] = value_max
                    labels = info['labels']
                    if value_max - value_min == 1:
                        options['is_toggle'] = True
                        if len(labels) != 2 or not info['enum']:
                            labels = ['off', 'on']
                    if len(labels) == value_max - value_min + 1:
                        options['labels'] = labels
                        options['ticks'] = list(range(value_min, value_max + 1))
                else:
                    options['value'] = float(info['value'])
                    options['value_default'] = float(info['default'])
                    options['value_min'] = float(info['min'])
                    options['value_max'] = float(info['max'])

                zctrls[symbol] = zynthian_controller(self, symbol, options)
                if info['bypass']:
                    self.bypass_zctrl = zctrls[symbol]

            # If control info is not OK
            except Exception as e:
                logging.error(f"Wrong parameter info from CLAP plugin '{self.plugin_name}' => {info} ({e})")

        # Setup zynthian bypass toggle
        if self.bypass_zctrl:
            # Reconfigure bypass zctrl to unify behaviour
            self.bypass_zctrl.set_options({"short_name": "bypass",
                                           "labels": ["inline", "bypass"],
                                           "ticks": [0, 1],
                                           "display_priority": 0})
        elif self.type == "Audio Effect":
            # Add host bypass zctrl
            self.bypass_zctrl = zctrls["bypass"] = zynthian_controller(self, 'bypass', {
                'name': "bypass",
                'is_toggle': True,
                'is_bypass': True,
                'bypass_value': 1,
                'value_max': 1,
                'value_default': 0,
                'value': 0,
                'processor': self,
                'labels': ['inline', 'bypass'],
                'ticks': [0, 1],
                "display_priority": 0
            })

        return zctrls

    def get_monitors_dict(self):
        return {}

    def get_controllers_dict(self, processor):
        # Get plugin static controllers
        zctrls = zynthian_engine.get_controllers_dict(self, processor)
        # Add plugin native controllers
        for zctrl in self.clap_zctrl_dict.values():
            zctrl.set_options({"processor": processor})
        zctrls.update(self.clap_zctrl_dict)
        return zctrls

    def send_controller_value(self, zctrl):
        if zctrl.graph_path is None:
            # Host bypass
            self.proc_cmd("bypass %d" % int(zctrl.value))
        else:
            self.proc_cmd("set %d %.6f" % (zctrl.graph_path, zctrl.value))

    # ----------------------------------------------------------------------------
    # Plugin state => Saved in snapshots & ZS3
    # ----------------------------------------------------------------------------

    def get_processor_state(self, processor):
        """ Get the plugin's state
        Returns: Opaque state, base64 encoded. None if it can't be got.
        """
        if not self.proc:
            return None
        self.state_event.clear()
        self.proc_cmd("state")
        if self.state_event.wait(self.STATE_TIMEOUT):
            return self.state_reply
        logging.error(f"Timeout getting state from CLAP plugin '{self.plugin_name}'")
        return None

    def set_processor_state(self, processor, state):
        """ Restore the plugin's state
        state: Opaque state, as returned by get_processor_state()
        """
        if self.proc and state:
            self.proc_cmd(f"load {state}")

# ******************************************************************************
