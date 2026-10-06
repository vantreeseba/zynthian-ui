#!/usr/bin/python3
# -*- coding: utf-8 -*-
# ********************************************************************
# ZYNTHIAN PROJECT: Zynthian CLAP-plugin management
#
# zynthian CLAP
#
# Copyright (C) 2026 Zynthian Project
#
# ********************************************************************
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
# ********************************************************************

import os
import json
import logging
from subprocess import run, PIPE

# ------------------------------------------------------------------------------
# Some variables & definitions
# ------------------------------------------------------------------------------

# Headless CLAP host. It's used for scanning & running the plugins.
ZYNCLAP_BIN = "{}/zynlibs/zynclap/build/zynclap".format(os.environ.get('ZYNTHIAN_UI_DIR', "/zynthian/zynthian-ui"))

# Max time for scanning all the plugins
SCAN_TIMEOUT = 120

# Engine category for each CLAP feature, by engine type.
# The first feature having a category is used, so plugins should list the most specific first.
clapfeature2engcat = {
    "MIDI Synth": {
        "synthesizer": "Synth",
        "sampler": "Sampler",
        "drum": "Percussion",
        "drum-machine": "Percussion"
    },
    "Audio Effect": {
        "analyzer": "Analyzer",
        "delay": "Delay",
        "glitch": "Delay",
        "granular": "Delay",
        "distortion": "Distortion",
        "compressor": "Dynamics",
        "de-esser": "Dynamics",
        "expander": "Dynamics",
        "gate": "Dynamics",
        "limiter": "Dynamics",
        "transient-shaper": "Dynamics",
        "equalizer": "Filter & EQ",
        "filter": "Filter & EQ",
        "chorus": "Modulation",
        "flanger": "Modulation",
        "phaser": "Modulation",
        "tremolo": "Modulation",
        "frequency-shifter": "Pitch",
        "pitch-correction": "Pitch",
        "pitch-shifter": "Pitch",
        "phase-vocoder": "Pitch",
        "reverb": "Reverb",
        "surround": "Spatial",
        "ambisonic": "Spatial"
    }
}

# ------------------------------------------------------------------------------
# CLAP plugin info functions
# ------------------------------------------------------------------------------


def scan_plugins():
    """ Scan the CLAP plugins installed in the system
    Returns: list of dictionaries => path, id, name, vendor, version, description & features of each plugin
    """
    if not os.path.isfile(ZYNCLAP_BIN):
        logging.debug(f"CLAP host not found in '{ZYNCLAP_BIN}'. Skipping CLAP plugins.")
        return []
    try:
        res = run([ZYNCLAP_BIN, "--scan"], stdout=PIPE, text=True, timeout=SCAN_TIMEOUT)
        return json.loads(res.stdout)
    except Exception as e:
        logging.error(f"Can't scan CLAP plugins => {e}")
        return []


def get_plugin_type(features):
    """ Get the engine type for a list of CLAP features
    Returns: engine type, as string
    """
    if "instrument" in features:
        return "MIDI Synth"
    elif "note-effect" in features:
        return "MIDI Tool"
    else:
        return "Audio Effect"


def get_plugin_cat(engine_type, features):
    """ Get the engine category for a list of CLAP features
    Returns: engine category, as string. "Other" if none of the features has a category.
    """
    feature2cat = clapfeature2engcat.get(engine_type, {})
    for feature in features:
        if feature in feature2cat:
            return feature2cat[feature]
    return "Other"

# ------------------------------------------------------------------------------
