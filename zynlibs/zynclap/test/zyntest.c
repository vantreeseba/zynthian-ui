/*
 * ******************************************************************
 * ZYNTHIAN PROJECT: Zynclap test plugin
 *
 * Minimal CLAP plugin used to test the zynclap host:
 * a stereo gain with a stepped mode, state and a note input port
 *
 * ******************************************************************
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU General Public License as
 * published by the Free Software Foundation; either version 2 of
 * the License, or any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU General Public License for more details.
 *
 * For a full copy of the GNU General Public License see the LICENSE.txt file.
 *
 * ******************************************************************
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <clap/clap.h>

enum {
    PARAM_GAIN = 1,
    PARAM_MODE = 2,
    PARAM_LAST_NOTE = 3,
    PARAM_COUNT = 3
};

enum {
    MODE_NORMAL = 0,
    MODE_MUTE = 1,
    MODE_INVERT = 2
};

static const char* mode_names[] = {"Normal", "Mute", "Invert"};

typedef struct {
    clap_plugin_t plugin;
    const clap_host_t* host;
    double gain;
    double mode;
    double last_note;
} zyntest_t;

static const char* features[] = {
    CLAP_PLUGIN_FEATURE_AUDIO_EFFECT,
    CLAP_PLUGIN_FEATURE_UTILITY,
    CLAP_PLUGIN_FEATURE_STEREO,
    NULL
};

static const clap_plugin_descriptor_t descriptor = {
    .clap_version = CLAP_VERSION_INIT,
    .id = "org.zynthian.zyntest",
    .name = "ZynTest",
    .vendor = "Zynthian",
    .url = "https://zynthian.org",
    .manual_url = "",
    .support_url = "",
    .version = "1.0.0",
    .description = "Test plugin for the zynclap host",
    .features = features
};

// -----------------------------------------------------------------------------
// Audio & note ports
// -----------------------------------------------------------------------------

static uint32_t audio_ports_count(const clap_plugin_t* plugin, bool is_input) {
    return 1;
}

static bool audio_ports_get(const clap_plugin_t* plugin, uint32_t index, bool is_input, clap_audio_port_info_t* info) {
    if (index > 0)
        return false;
    info->id = is_input ? 0 : 1;
    snprintf(info->name, sizeof(info->name), "%s", is_input ? "Input" : "Output");
    info->flags = CLAP_AUDIO_PORT_IS_MAIN;
    info->channel_count = 2;
    info->port_type = CLAP_PORT_STEREO;
    info->in_place_pair = CLAP_INVALID_ID;
    return true;
}

static const clap_plugin_audio_ports_t audio_ports = {
    .count = audio_ports_count,
    .get = audio_ports_get
};

static uint32_t note_ports_count(const clap_plugin_t* plugin, bool is_input) {
    return is_input ? 1 : 0;
}

static bool note_ports_get(const clap_plugin_t* plugin, uint32_t index, bool is_input, clap_note_port_info_t* info) {
    if (!is_input || index > 0)
        return false;
    info->id = 0;
    info->supported_dialects = CLAP_NOTE_DIALECT_MIDI;
    info->preferred_dialect = CLAP_NOTE_DIALECT_MIDI;
    snprintf(info->name, sizeof(info->name), "%s", "Notes");
    return true;
}

static const clap_plugin_note_ports_t note_ports = {
    .count = note_ports_count,
    .get = note_ports_get
};

// -----------------------------------------------------------------------------
// Parameters
// -----------------------------------------------------------------------------

static uint32_t params_count(const clap_plugin_t* plugin) {
    return PARAM_COUNT;
}

static bool params_get_info(const clap_plugin_t* plugin, uint32_t index, clap_param_info_t* info) {
    memset(info, 0, sizeof(*info));
    switch (index) {
        case 0:
            info->id = PARAM_GAIN;
            info->flags = CLAP_PARAM_IS_AUTOMATABLE;
            snprintf(info->name, sizeof(info->name), "%s", "Gain");
            info->min_value = 0.0;
            info->max_value = 2.0;
            info->default_value = 1.0;
            break;
        case 1:
            info->id = PARAM_MODE;
            info->flags = CLAP_PARAM_IS_AUTOMATABLE | CLAP_PARAM_IS_STEPPED | CLAP_PARAM_IS_ENUM;
            snprintf(info->name, sizeof(info->name), "%s", "Mode");
            info->min_value = 0.0;
            info->max_value = 2.0;
            info->default_value = 0.0;
            break;
        case 2:
            info->id = PARAM_LAST_NOTE;
            info->flags = CLAP_PARAM_IS_STEPPED | CLAP_PARAM_IS_READONLY;
            snprintf(info->name, sizeof(info->name), "%s", "Last Note");
            info->min_value = 0.0;
            info->max_value = 127.0;
            info->default_value = 0.0;
            break;
        default:
            return false;
    }
    snprintf(info->module, sizeof(info->module), "%s", "Main");
    return true;
}

static bool params_get_value(const clap_plugin_t* plugin, clap_id param_id, double* value) {
    zyntest_t* self = plugin->plugin_data;
    switch (param_id) {
        case PARAM_GAIN:
            *value = self->gain;
            return true;
        case PARAM_MODE:
            *value = self->mode;
            return true;
        case PARAM_LAST_NOTE:
            *value = self->last_note;
            return true;
    }
    return false;
}

static bool params_value_to_text(const clap_plugin_t* plugin, clap_id param_id, double value, char* out, uint32_t size) {
    switch (param_id) {
        case PARAM_GAIN:
            snprintf(out, size, "%.2f", value);
            return true;
        case PARAM_MODE:
            if (value < 0.0 || value > 2.0)
                return false;
            snprintf(out, size, "%s", mode_names[(int)value]);
            return true;
        case PARAM_LAST_NOTE:
            snprintf(out, size, "%d", (int)value);
            return true;
    }
    return false;
}

static bool params_text_to_value(const clap_plugin_t* plugin, clap_id param_id, const char* text, double* value) {
    if (param_id == PARAM_MODE) {
        for (int i = 0; i < 3; ++i) {
            if (!strcmp(text, mode_names[i])) {
                *value = i;
                return true;
            }
        }
        return false;
    }
    *value = atof(text);
    return true;
}

static void handle_event(zyntest_t* self, const clap_event_header_t* header, const clap_output_events_t* out) {
    if (header->space_id != CLAP_CORE_EVENT_SPACE_ID)
        return;
    if (header->type == CLAP_EVENT_PARAM_VALUE) {
        const clap_event_param_value_t* event = (const clap_event_param_value_t*)header;
        if (event->param_id == PARAM_GAIN)
            self->gain = event->value;
        else if (event->param_id == PARAM_MODE)
            self->mode = event->value;
    } else if (header->type == CLAP_EVENT_MIDI) {
        const clap_event_midi_t* event = (const clap_event_midi_t*)header;
        if ((event->data[0] & 0xF0) == 0x90 && event->data[2] > 0) {
            self->last_note = event->data[1];
            // Tell the host the read-only parameter has changed
            clap_event_param_value_t fb;
            memset(&fb, 0, sizeof(fb));
            fb.header.size = sizeof(fb);
            fb.header.time = header->time;
            fb.header.space_id = CLAP_CORE_EVENT_SPACE_ID;
            fb.header.type = CLAP_EVENT_PARAM_VALUE;
            fb.param_id = PARAM_LAST_NOTE;
            fb.note_id = -1;
            fb.port_index = -1;
            fb.channel = -1;
            fb.key = -1;
            fb.value = self->last_note;
            out->try_push(out, &fb.header);
        }
    }
}

static void params_flush(const clap_plugin_t* plugin, const clap_input_events_t* in, const clap_output_events_t* out) {
    zyntest_t* self = plugin->plugin_data;
    uint32_t count = in->size(in);
    for (uint32_t i = 0; i < count; ++i)
        handle_event(self, in->get(in, i), out);
}

static const clap_plugin_params_t params = {
    .count = params_count,
    .get_info = params_get_info,
    .get_value = params_get_value,
    .value_to_text = params_value_to_text,
    .text_to_value = params_text_to_value,
    .flush = params_flush
};

// -----------------------------------------------------------------------------
// State
// -----------------------------------------------------------------------------

static bool state_save(const clap_plugin_t* plugin, const clap_ostream_t* stream) {
    zyntest_t* self = plugin->plugin_data;
    double values[2] = {self->gain, self->mode};
    return stream->write(stream, values, sizeof(values)) == sizeof(values);
}

static bool state_load(const clap_plugin_t* plugin, const clap_istream_t* stream) {
    zyntest_t* self = plugin->plugin_data;
    double values[2];
    if (stream->read(stream, values, sizeof(values)) != sizeof(values))
        return false;
    self->gain = values[0];
    self->mode = values[1];
    return true;
}

static const clap_plugin_state_t state = {
    .save = state_save,
    .load = state_load
};

// -----------------------------------------------------------------------------
// Plugin
// -----------------------------------------------------------------------------

static bool plugin_init(const clap_plugin_t* plugin) {
    return true;
}

static void plugin_destroy(const clap_plugin_t* plugin) {
    free(plugin->plugin_data);
}

static bool plugin_activate(const clap_plugin_t* plugin, double sample_rate, uint32_t min_frames, uint32_t max_frames) {
    return true;
}

static void plugin_deactivate(const clap_plugin_t* plugin) {
}

static bool plugin_start_processing(const clap_plugin_t* plugin) {
    return true;
}

static void plugin_stop_processing(const clap_plugin_t* plugin) {
}

static void plugin_reset(const clap_plugin_t* plugin) {
}

static clap_process_status plugin_process(const clap_plugin_t* plugin, const clap_process_t* process) {
    zyntest_t* self = plugin->plugin_data;

    uint32_t count = process->in_events->size(process->in_events);
    for (uint32_t i = 0; i < count; ++i)
        handle_event(self, process->in_events->get(process->in_events, i), process->out_events);

    float gain = (float)self->gain;
    if (self->mode == MODE_MUTE)
        gain = 0.0f;
    else if (self->mode == MODE_INVERT)
        gain = -gain;

    for (uint32_t ch = 0; ch < 2; ++ch) {
        const float* in = process->audio_inputs[0].data32[ch];
        float* out = process->audio_outputs[0].data32[ch];
        for (uint32_t i = 0; i < process->frames_count; ++i)
            out[i] = in[i] * gain;
    }
    return CLAP_PROCESS_CONTINUE;
}

static const void* plugin_get_extension(const clap_plugin_t* plugin, const char* id) {
    if (!strcmp(id, CLAP_EXT_AUDIO_PORTS))
        return &audio_ports;
    if (!strcmp(id, CLAP_EXT_NOTE_PORTS))
        return &note_ports;
    if (!strcmp(id, CLAP_EXT_PARAMS))
        return &params;
    if (!strcmp(id, CLAP_EXT_STATE))
        return &state;
    return NULL;
}

static void plugin_on_main_thread(const clap_plugin_t* plugin) {
}

// -----------------------------------------------------------------------------
// Factory & entry
// -----------------------------------------------------------------------------

static uint32_t factory_get_plugin_count(const clap_plugin_factory_t* factory) {
    return 1;
}

static const clap_plugin_descriptor_t* factory_get_plugin_descriptor(const clap_plugin_factory_t* factory, uint32_t index) {
    return index == 0 ? &descriptor : NULL;
}

static const clap_plugin_t* factory_create_plugin(const clap_plugin_factory_t* factory, const clap_host_t* host, const char* plugin_id) {
    if (strcmp(plugin_id, descriptor.id))
        return NULL;
    zyntest_t* self = calloc(1, sizeof(zyntest_t));
    if (!self)
        return NULL;
    self->host = host;
    self->gain = 1.0;
    self->plugin.desc = &descriptor;
    self->plugin.plugin_data = self;
    self->plugin.init = plugin_init;
    self->plugin.destroy = plugin_destroy;
    self->plugin.activate = plugin_activate;
    self->plugin.deactivate = plugin_deactivate;
    self->plugin.start_processing = plugin_start_processing;
    self->plugin.stop_processing = plugin_stop_processing;
    self->plugin.reset = plugin_reset;
    self->plugin.process = plugin_process;
    self->plugin.get_extension = plugin_get_extension;
    self->plugin.on_main_thread = plugin_on_main_thread;
    return &self->plugin;
}

static const clap_plugin_factory_t factory = {
    .get_plugin_count = factory_get_plugin_count,
    .get_plugin_descriptor = factory_get_plugin_descriptor,
    .create_plugin = factory_create_plugin
};

static bool entry_init(const char* path) {
    return true;
}

static void entry_deinit(void) {
}

static const void* entry_get_factory(const char* factory_id) {
    if (!strcmp(factory_id, CLAP_PLUGIN_FACTORY_ID))
        return &factory;
    return NULL;
}

CLAP_EXPORT const clap_plugin_entry_t clap_entry = {
    .clap_version = CLAP_VERSION_INIT,
    .init = entry_init,
    .deinit = entry_deinit,
    .get_factory = entry_get_factory
};
