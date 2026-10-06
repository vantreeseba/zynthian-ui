/*
 * ******************************************************************
 * ZYNTHIAN PROJECT: Zynclap
 *
 * Headless JACK host for CLAP plugins.
 * It runs a single plugin and it's controlled by a line protocol on
 * stdin/stdout, similar to the one used by zynthian's jalv:
 *
 *   host => UI
 *     #PRM> {json}        Parameter description (one per parameter)
 *     #CTR> <id>=<value>  The plugin changed a parameter value
 *     #STA> <base64>      Plugin state (reply to "state")
 *     >                   Prompt: startup or command completed
 *
 *   UI => host
 *     set <id> <value>    Set a parameter value
 *     state               Get the plugin state
 *     load <base64>       Restore the plugin state
 *     params              Send again the parameter descriptions
 *     bypass <0|1>        Pass the audio inputs to the outputs, untouched
 *
 * Copyright (C) 2026 Zynthian Project
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

#include <atomic>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <set>
#include <string>
#include <vector>

#include <dlfcn.h>
#include <poll.h>
#include <pthread.h>
#include <sys/wait.h>
#include <unistd.h>

#include <jack/jack.h>
#include <jack/midiport.h>
#include <jack/ringbuffer.h>

#include <clap/clap.h>

#define MAX_EVENTS 4096           // Max events sent to the plugin in a period
#define RING_MESSAGES 4096        // Size of the parameter queues, in messages
#define MAX_LABELLED_STEPS 64     // Stepped parameters with more steps than this get no value labels
#define MAIN_LOOP_PERIOD_MS 20    // Period for polling plugin feedback & requests
#define SCAN_TIMEOUT_S 15         // Max time for scanning a single plugin file
#define RESTART_TIMEOUT_MS 1000   // Max time waiting the audio thread to pause

// -----------------------------------------------------------------------------
// Types
// -----------------------------------------------------------------------------

// Parameter value travelling between the main and the audio thread
struct param_msg_t {
    clap_id id;
    void* cookie;
    double value;
};

// Parameter cache, owned by the main thread
struct param_t {
    clap_param_info_t info;
    double value;
};

union event_t {
    clap_event_header_t header;
    clap_event_param_value_t param;
    clap_event_midi_t midi;
    clap_event_note_t note;
};

// A plugin's audio port and the JACK ports carrying its channels
struct audio_port_t {
    std::vector<jack_port_t*> jack_ports;
    std::vector<float*> buffers;
};

// -----------------------------------------------------------------------------
// Global state
// -----------------------------------------------------------------------------

static const clap_plugin_entry_t* entry = nullptr;
static const clap_plugin_t* plugin = nullptr;
static const clap_plugin_params_t* plugin_params = nullptr;
static const clap_plugin_state_t* plugin_state = nullptr;

static jack_client_t* jack_client = nullptr;
static jack_port_t* midi_in_port = nullptr;
static jack_port_t* midi_out_port = nullptr;
static bool midi_in_as_midi = true;       // false: convert MIDI notes to CLAP note events
static void* midi_out_buffer = nullptr;   // Only valid inside the process callback

static std::vector<audio_port_t> audio_inputs;
static std::vector<audio_port_t> audio_outputs;
static std::vector<clap_audio_buffer_t> clap_inputs;
static std::vector<clap_audio_buffer_t> clap_outputs;

static event_t in_events[MAX_EVENTS];
static uint32_t in_events_count = 0;
static int64_t steady_time = 0;

static jack_ringbuffer_t* ring_to_audio = nullptr;   // main => audio: parameter changes
static jack_ringbuffer_t* ring_to_main = nullptr;    // audio => main: parameter feedback

static std::vector<param_t> params;

static pthread_t main_thread;
static thread_local bool is_audio_thread = false;

static std::atomic<bool> quit{false};
static std::atomic<bool> callback_requested{false};
static std::atomic<bool> restart_requested{false};
static std::atomic<bool> rescan_requested{false};
static std::atomic<bool> pause_requested{false};
static std::atomic<bool> paused{false};
static std::atomic<bool> active{false};
static std::atomic<bool> bypassed{false};
static bool processing = false;   // Owned by the audio thread

// -----------------------------------------------------------------------------
// Text helpers
// -----------------------------------------------------------------------------

static std::string json_string(const char* text) {
    std::string res = "\"";
    for (const char* c = text ? text : ""; *c; ++c) {
        switch (*c) {
            case '"':
                res += "\\\"";
                break;
            case '\\':
                res += "\\\\";
                break;
            case '\n':
                res += "\\n";
                break;
            case '\r':
                res += "\\r";
                break;
            case '\t':
                res += "\\t";
                break;
            default:
                if ((unsigned char)*c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", *c);
                    res += buf;
                } else
                    res += *c;
        }
    }
    return res + "\"";
}

static std::string json_number(double value) {
    if (!std::isfinite(value))
        return "0";
    char buf[32];
    snprintf(buf, sizeof(buf), "%.9g", value);
    return buf;
}

static const char base64_chars[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static std::string base64_encode(const std::vector<uint8_t>& data) {
    std::string res;
    res.reserve((data.size() + 2) / 3 * 4);
    for (size_t i = 0; i < data.size(); i += 3) {
        uint32_t chunk = data[i] << 16;
        if (i + 1 < data.size())
            chunk |= data[i + 1] << 8;
        if (i + 2 < data.size())
            chunk |= data[i + 2];
        res += base64_chars[(chunk >> 18) & 0x3F];
        res += base64_chars[(chunk >> 12) & 0x3F];
        res += i + 1 < data.size() ? base64_chars[(chunk >> 6) & 0x3F] : '=';
        res += i + 2 < data.size() ? base64_chars[chunk & 0x3F] : '=';
    }
    return res;
}

static std::vector<uint8_t> base64_decode(const std::string& text) {
    std::vector<uint8_t> res;
    uint32_t chunk = 0;
    int bits = 0;
    for (char c : text) {
        const char* pos = c ? strchr(base64_chars, c) : nullptr;
        if (!pos)
            continue;
        chunk = (chunk << 6) | (uint32_t)(pos - base64_chars);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            res.push_back((chunk >> bits) & 0xFF);
        }
    }
    return res;
}

static void print_line(const std::string& line) {
    fputs(line.c_str(), stdout);
    fputc('\n', stdout);
    fflush(stdout);
}

// -----------------------------------------------------------------------------
// Plugin file loading
// -----------------------------------------------------------------------------

// Load a plugin file and get its factory. Returns nullptr on failure.
static const clap_plugin_factory_t* load_plugin_file(const char* path) {
    void* lib = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!lib) {
        fprintf(stderr, "zynclap: can't load '%s': %s\n", path, dlerror());
        return nullptr;
    }
    entry = (const clap_plugin_entry_t*)dlsym(lib, "clap_entry");
    if (!entry) {
        fprintf(stderr, "zynclap: '%s' is not a CLAP plugin\n", path);
        return nullptr;
    }
    if (!clap_version_is_compatible(entry->clap_version) || !entry->init(path)) {
        fprintf(stderr, "zynclap: can't initialize '%s'\n", path);
        entry = nullptr;
        return nullptr;
    }
    return (const clap_plugin_factory_t*)entry->get_factory(CLAP_PLUGIN_FACTORY_ID);
}

// -----------------------------------------------------------------------------
// Scan mode
// -----------------------------------------------------------------------------

static std::vector<std::string> get_search_paths() {
    std::vector<std::string> paths;
    std::set<std::string> seen;
    auto add = [&](const std::string& path) {
        if (!path.empty() && seen.insert(path).second)
            paths.push_back(path);
    };
    if (const char* env = getenv("CLAP_PATH")) {
        std::string list = env;
        size_t start = 0;
        while (start <= list.size()) {
            size_t end = list.find(':', start);
            if (end == std::string::npos)
                end = list.size();
            add(list.substr(start, end - start));
            start = end + 1;
        }
    }
    if (const char* home = getenv("HOME"))
        add(std::string(home) + "/.clap");
    add("/usr/lib/clap");
    add("/usr/local/lib/clap");
    return paths;
}

static std::vector<std::string> find_plugin_files() {
    std::vector<std::string> files;
    std::set<std::string> seen;
    for (const std::string& dir : get_search_paths()) {
        std::error_code err;
        auto opts = std::filesystem::directory_options::follow_directory_symlink |
            std::filesystem::directory_options::skip_permission_denied;
        std::filesystem::recursive_directory_iterator it(dir, opts, err), end;
        for (; !err && it != end; it.increment(err)) {
            if (it->path().extension() != ".clap" || !it->is_regular_file(err))
                continue;
            std::string real = std::filesystem::canonical(it->path(), err).string();
            if (!err && seen.insert(real).second)
                files.push_back(it->path().string());
        }
    }
    return files;
}

// Write the descriptors of a plugin file as comma-separated JSON objects.
// It runs in a child process, as loading a plugin may crash or hang.
static void scan_plugin_file(const std::string& path, FILE* out) {
    const clap_plugin_factory_t* factory = load_plugin_file(path.c_str());
    if (!factory)
        return;
    uint32_t count = factory->get_plugin_count(factory);
    for (uint32_t i = 0; i < count; ++i) {
        const clap_plugin_descriptor_t* desc = factory->get_plugin_descriptor(factory, i);
        if (!desc || !desc->id || !desc->name)
            continue;
        std::string features;
        for (const char* const* feature = desc->features; feature && *feature; ++feature) {
            if (!features.empty())
                features += ",";
            features += json_string(*feature);
        }
        fprintf(out, "%s{\"path\":%s,\"id\":%s,\"name\":%s,\"vendor\":%s,\"version\":%s,\"description\":%s,\"features\":[%s]}",
            i ? "," : "",
            json_string(path.c_str()).c_str(),
            json_string(desc->id).c_str(),
            json_string(desc->name).c_str(),
            json_string(desc->vendor).c_str(),
            json_string(desc->version).c_str(),
            json_string(desc->description).c_str(),
            features.c_str());
    }
    fflush(out);
}

static int scan() {
    std::string result;
    for (const std::string& path : find_plugin_files()) {
        int fds[2];
        if (pipe(fds))
            continue;
        pid_t pid = fork();
        if (pid < 0) {
            close(fds[0]);
            close(fds[1]);
            continue;
        }
        if (pid == 0) {
            close(fds[0]);
            // Anything the plugin prints must not end in the scan result
            dup2(STDERR_FILENO, STDOUT_FILENO);
            alarm(SCAN_TIMEOUT_S);
            FILE* out = fdopen(fds[1], "w");
            scan_plugin_file(path, out);
            _exit(0);
        }
        close(fds[1]);
        std::string output;
        char buf[4096];
        ssize_t n;
        while ((n = read(fds[0], buf, sizeof(buf))) > 0)
            output.append(buf, n);
        close(fds[0]);
        int status = 0;
        waitpid(pid, &status, 0);
        if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
            fprintf(stderr, "zynclap: scanning '%s' failed\n", path.c_str());
            continue;
        }
        if (output.empty())
            continue;
        if (!result.empty())
            result += ",\n";
        result += output;
    }
    print_line("[" + result + "]");
    return 0;
}

// -----------------------------------------------------------------------------
// CLAP host
// -----------------------------------------------------------------------------

static void host_log(const clap_host_t* host, clap_log_severity severity, const char* msg) {
    fprintf(stderr, "zynclap: plugin log (%d): %s\n", severity, msg);
}

static bool host_is_main_thread(const clap_host_t* host) {
    return pthread_equal(pthread_self(), main_thread);
}

static bool host_is_audio_thread(const clap_host_t* host) {
    return is_audio_thread;
}

static void host_params_rescan(const clap_host_t* host, clap_param_rescan_flags flags) {
    rescan_requested = true;
}

static void host_params_clear(const clap_host_t* host, clap_id param_id, clap_param_clear_flags flags) {
}

static void host_params_request_flush(const clap_host_t* host) {
    // Nothing to do: the plugin is always processing, so events are always flushed
}

static void host_state_mark_dirty(const clap_host_t* host) {
}

static const clap_host_log_t host_log_ext = {host_log};
static const clap_host_thread_check_t host_thread_check_ext = {host_is_main_thread, host_is_audio_thread};
static const clap_host_params_t host_params_ext = {host_params_rescan, host_params_clear, host_params_request_flush};
static const clap_host_state_t host_state_ext = {host_state_mark_dirty};

static const void* host_get_extension(const clap_host_t* host, const char* id) {
    if (!strcmp(id, CLAP_EXT_LOG))
        return &host_log_ext;
    if (!strcmp(id, CLAP_EXT_THREAD_CHECK))
        return &host_thread_check_ext;
    if (!strcmp(id, CLAP_EXT_PARAMS))
        return &host_params_ext;
    if (!strcmp(id, CLAP_EXT_STATE))
        return &host_state_ext;
    return nullptr;
}

static void host_request_restart(const clap_host_t* host) {
    restart_requested = true;
}

static void host_request_process(const clap_host_t* host) {
    // Nothing to do: the plugin is always processing
}

static void host_request_callback(const clap_host_t* host) {
    callback_requested = true;
}

static const clap_host_t host = {
    CLAP_VERSION_INIT,
    nullptr,
    "zynclap",
    "Zynthian",
    "https://zynthian.org",
    "1.0.0",
    host_get_extension,
    host_request_restart,
    host_request_process,
    host_request_callback
};

// -----------------------------------------------------------------------------
// Events
// -----------------------------------------------------------------------------

static uint32_t in_events_size(const clap_input_events_t* list) {
    return in_events_count;
}

static const clap_event_header_t* in_events_get(const clap_input_events_t* list, uint32_t index) {
    return index < in_events_count ? &in_events[index].header : nullptr;
}

static void write_midi_out(uint32_t time, const uint8_t* data, size_t size) {
    if (midi_out_buffer)
        jack_midi_event_write(midi_out_buffer, time, data, size);
}

static bool out_events_try_push(const clap_output_events_t* list, const clap_event_header_t* header) {
    if (header->space_id != CLAP_CORE_EVENT_SPACE_ID)
        return true;
    switch (header->type) {
        case CLAP_EVENT_PARAM_VALUE: {
            const clap_event_param_value_t* event = (const clap_event_param_value_t*)header;
            param_msg_t msg = {event->param_id, nullptr, event->value};
            if (jack_ringbuffer_write_space(ring_to_main) < sizeof(msg))
                return false;
            jack_ringbuffer_write(ring_to_main, (const char*)&msg, sizeof(msg));
            break;
        }
        case CLAP_EVENT_MIDI: {
            const clap_event_midi_t* event = (const clap_event_midi_t*)header;
            uint8_t status = event->data[0] & 0xF0;
            write_midi_out(header->time, event->data, status == 0xC0 || status == 0xD0 ? 2 : 3);
            break;
        }
        case CLAP_EVENT_NOTE_ON:
        case CLAP_EVENT_NOTE_OFF: {
            const clap_event_note_t* event = (const clap_event_note_t*)header;
            if (event->channel < 0 || event->key < 0)
                break;
            bool on = header->type == CLAP_EVENT_NOTE_ON;
            uint8_t data[3] = {
                (uint8_t)((on ? 0x90 : 0x80) | (event->channel & 0x0F)),
                (uint8_t)(event->key & 0x7F),
                (uint8_t)(event->velocity * 127.0)
            };
            if (on && data[2] == 0)
                data[2] = 1;
            write_midi_out(header->time, data, 3);
            break;
        }
    }
    return true;
}

static const clap_input_events_t in_events_list = {nullptr, in_events_size, in_events_get};
static const clap_output_events_t out_events_list = {nullptr, out_events_try_push};

static void init_event_header(clap_event_header_t* header, uint32_t size, uint32_t time, uint16_t type) {
    header->size = size;
    header->time = time;
    header->space_id = CLAP_CORE_EVENT_SPACE_ID;
    header->type = type;
    header->flags = 0;
}

// Queue the parameter changes requested by the main thread
static void push_param_events() {
    param_msg_t msg;
    while (in_events_count < MAX_EVENTS && jack_ringbuffer_read_space(ring_to_audio) >= sizeof(msg)) {
        jack_ringbuffer_read(ring_to_audio, (char*)&msg, sizeof(msg));
        clap_event_param_value_t* event = &in_events[in_events_count++].param;
        init_event_header(&event->header, sizeof(*event), 0, CLAP_EVENT_PARAM_VALUE);
        event->param_id = msg.id;
        event->cookie = msg.cookie;
        event->note_id = -1;
        event->port_index = -1;
        event->channel = -1;
        event->key = -1;
        event->value = msg.value;
    }
}

// Queue the MIDI received from JACK
static void push_midi_events(jack_nframes_t frames) {
    if (!midi_in_port)
        return;
    void* buffer = jack_port_get_buffer(midi_in_port, frames);
    uint32_t count = jack_midi_get_event_count(buffer);
    jack_midi_event_t midi;
    for (uint32_t i = 0; i < count && in_events_count < MAX_EVENTS; ++i) {
        if (jack_midi_event_get(&midi, buffer, i) || midi.size < 1 || midi.size > 3)
            continue;
        if (midi_in_as_midi) {
            clap_event_midi_t* event = &in_events[in_events_count++].midi;
            init_event_header(&event->header, sizeof(*event), midi.time, CLAP_EVENT_MIDI);
            event->port_index = 0;
            event->data[0] = midi.buffer[0];
            event->data[1] = midi.size > 1 ? midi.buffer[1] : 0;
            event->data[2] = midi.size > 2 ? midi.buffer[2] : 0;
            continue;
        }
        uint8_t status = midi.buffer[0] & 0xF0;
        if (midi.size != 3 || (status != 0x90 && status != 0x80))
            continue;
        bool on = status == 0x90 && midi.buffer[2] > 0;
        clap_event_note_t* event = &in_events[in_events_count++].note;
        init_event_header(&event->header, sizeof(*event), midi.time, on ? CLAP_EVENT_NOTE_ON : CLAP_EVENT_NOTE_OFF);
        event->note_id = -1;
        event->port_index = 0;
        event->channel = midi.buffer[0] & 0x0F;
        event->key = midi.buffer[1];
        event->velocity = midi.buffer[2] / 127.0;
    }
}

static void fill_transport(clap_event_transport_t* transport) {
    memset(transport, 0, sizeof(*transport));
    init_event_header(&transport->header, sizeof(*transport), 0, CLAP_EVENT_TRANSPORT);
    jack_position_t pos;
    if (jack_transport_query(jack_client, &pos) == JackTransportRolling)
        transport->flags |= CLAP_TRANSPORT_IS_PLAYING;
    if (pos.frame_rate) {
        transport->flags |= CLAP_TRANSPORT_HAS_SECONDS_TIMELINE;
        transport->song_pos_seconds = (clap_sectime)((double)pos.frame / pos.frame_rate * CLAP_SECTIME_FACTOR);
    }
    if ((pos.valid & JackPositionBBT) && pos.ticks_per_beat > 0) {
        transport->flags |= CLAP_TRANSPORT_HAS_TEMPO | CLAP_TRANSPORT_HAS_BEATS_TIMELINE | CLAP_TRANSPORT_HAS_TIME_SIGNATURE;
        transport->tempo = pos.beats_per_minute;
        double bar_start = (pos.bar - 1) * pos.beats_per_bar;
        double beats = bar_start + (pos.beat - 1) + pos.tick / pos.ticks_per_beat;
        transport->song_pos_beats = (clap_beattime)(beats * CLAP_BEATTIME_FACTOR);
        transport->bar_start = (clap_beattime)(bar_start * CLAP_BEATTIME_FACTOR);
        transport->bar_number = pos.bar - 1;
        transport->tsig_num = (uint16_t)pos.beats_per_bar;
        transport->tsig_denom = (uint16_t)pos.beat_type;
    }
}

// -----------------------------------------------------------------------------
// JACK callbacks
// -----------------------------------------------------------------------------

static void write_silence(jack_nframes_t frames) {
    for (audio_port_t& port : audio_outputs)
        for (float* buffer : port.buffers)
            memset(buffer, 0, frames * sizeof(float));
}

// Copy each input channel to the output channel with the same position
static void write_passthrough(jack_nframes_t frames) {
    size_t in_port = 0, in_ch = 0;
    for (audio_port_t& port : audio_outputs) {
        for (float* buffer : port.buffers) {
            while (in_port < audio_inputs.size() && in_ch >= audio_inputs[in_port].buffers.size()) {
                ++in_port;
                in_ch = 0;
            }
            if (in_port < audio_inputs.size())
                memcpy(buffer, audio_inputs[in_port].buffers[in_ch++], frames * sizeof(float));
            else
                memset(buffer, 0, frames * sizeof(float));
        }
    }
}

static int on_jack_process(jack_nframes_t frames, void* arg) {
    is_audio_thread = true;

    for (audio_port_t& port : audio_inputs)
        for (size_t ch = 0; ch < port.jack_ports.size(); ++ch)
            port.buffers[ch] = (float*)jack_port_get_buffer(port.jack_ports[ch], frames);
    for (audio_port_t& port : audio_outputs)
        for (size_t ch = 0; ch < port.jack_ports.size(); ++ch)
            port.buffers[ch] = (float*)jack_port_get_buffer(port.jack_ports[ch], frames);
    midi_out_buffer = midi_out_port ? jack_port_get_buffer(midi_out_port, frames) : nullptr;
    if (midi_out_buffer)
        jack_midi_clear_buffer(midi_out_buffer);

    if (pause_requested || !active) {
        if (processing) {
            plugin->stop_processing(plugin);
            processing = false;
        }
        paused = true;
        write_silence(frames);
        return 0;
    }
    paused = false;
    if (!processing) {
        processing = plugin->start_processing(plugin);
        if (!processing) {
            write_silence(frames);
            return 0;
        }
    }

    in_events_count = 0;
    push_param_events();
    push_midi_events(frames);

    clap_event_transport_t transport;
    fill_transport(&transport);

    clap_process_t process;
    memset(&process, 0, sizeof(process));
    process.steady_time = steady_time;
    process.frames_count = frames;
    process.transport = &transport;
    process.audio_inputs = clap_inputs.data();
    process.audio_outputs = clap_outputs.data();
    process.audio_inputs_count = clap_inputs.size();
    process.audio_outputs_count = clap_outputs.size();
    process.in_events = &in_events_list;
    process.out_events = &out_events_list;
    // The plugin keeps processing while bypassed, so its parameters and state stay up to date
    if (plugin->process(plugin, &process) == CLAP_PROCESS_ERROR)
        write_silence(frames);
    if (bypassed)
        write_passthrough(frames);
    steady_time += frames;
    return 0;
}

static int on_jack_config_change(jack_nframes_t value, void* arg) {
    // Sample rate or buffer size changed: the plugin must be activated again
    if (active)
        restart_requested = true;
    return 0;
}

static void on_jack_shutdown(void* arg) {
    quit = true;
}

// -----------------------------------------------------------------------------
// Plugin setup
// -----------------------------------------------------------------------------

static bool register_audio_ports(bool is_input) {
    const clap_plugin_audio_ports_t* ext = (const clap_plugin_audio_ports_t*)plugin->get_extension(plugin, CLAP_EXT_AUDIO_PORTS);
    std::vector<audio_port_t>& ports = is_input ? audio_inputs : audio_outputs;
    uint32_t count = ext ? ext->count(plugin, is_input) : 0;
    uint32_t channel = 1;
    for (uint32_t i = 0; i < count; ++i) {
        clap_audio_port_info_t info;
        if (!ext->get(plugin, i, is_input, &info))
            return false;
        audio_port_t port;
        for (uint32_t ch = 0; ch < info.channel_count; ++ch) {
            std::string name = (is_input ? "in_" : "out_") + std::to_string(channel++);
            jack_port_t* jack_port = jack_port_register(jack_client, name.c_str(), JACK_DEFAULT_AUDIO_TYPE,
                is_input ? JackPortIsInput : JackPortIsOutput, 0);
            if (!jack_port)
                return false;
            port.jack_ports.push_back(jack_port);
            port.buffers.push_back(nullptr);
        }
        ports.push_back(port);
    }
    // Buffer pointers are taken once all ports exist, so the vectors don't move anymore
    std::vector<clap_audio_buffer_t>& clap_buffers = is_input ? clap_inputs : clap_outputs;
    for (audio_port_t& port : ports) {
        clap_audio_buffer_t buffer;
        memset(&buffer, 0, sizeof(buffer));
        buffer.data32 = port.buffers.data();
        buffer.channel_count = port.buffers.size();
        clap_buffers.push_back(buffer);
    }
    return true;
}

static bool register_midi_ports() {
    const clap_plugin_note_ports_t* ext = (const clap_plugin_note_ports_t*)plugin->get_extension(plugin, CLAP_EXT_NOTE_PORTS);
    if (!ext)
        return true;
    clap_note_port_info_t info;
    if (ext->count(plugin, true) > 0 && ext->get(plugin, 0, true, &info)) {
        midi_in_as_midi = info.supported_dialects & CLAP_NOTE_DIALECT_MIDI;
        midi_in_port = jack_port_register(jack_client, "midi_in", JACK_DEFAULT_MIDI_TYPE, JackPortIsInput, 0);
        if (!midi_in_port)
            return false;
    }
    if (ext->count(plugin, false) > 0) {
        midi_out_port = jack_port_register(jack_client, "midi_out", JACK_DEFAULT_MIDI_TYPE, JackPortIsOutput, 0);
        if (!midi_out_port)
            return false;
    }
    return true;
}

static bool activate_plugin() {
    jack_nframes_t buffer_size = jack_get_buffer_size(jack_client);
    active = plugin->activate(plugin, jack_get_sample_rate(jack_client), 1, buffer_size);
    if (!active)
        fprintf(stderr, "zynclap: can't activate plugin\n");
    return active;
}

// Stop the audio thread from using the plugin. Returns once it's safe to call main-thread only functions.
static void pause_processing() {
    pause_requested = true;
    for (int waited = 0; !paused && waited < RESTART_TIMEOUT_MS; ++waited)
        usleep(1000);
}

static void restart_plugin() {
    pause_processing();
    if (active) {
        plugin->deactivate(plugin);
        active = false;
    }
    activate_plugin();
    pause_requested = false;
}

// -----------------------------------------------------------------------------
// Parameters & state (main thread)
// -----------------------------------------------------------------------------

static void load_params() {
    params.clear();
    if (!plugin_params)
        return;
    uint32_t count = plugin_params->count(plugin);
    for (uint32_t i = 0; i < count; ++i) {
        param_t param;
        if (!plugin_params->get_info(plugin, i, &param.info))
            continue;
        if (!plugin_params->get_value(plugin, param.info.id, &param.value))
            param.value = param.info.default_value;
        params.push_back(param);
    }
}

static std::string get_param_labels(const param_t& param) {
    std::string labels;
    double steps = param.info.max_value - param.info.min_value;
    if (!(param.info.flags & CLAP_PARAM_IS_STEPPED) || steps < 1 || steps > MAX_LABELLED_STEPS)
        return labels;
    char text[256];
    for (double value = param.info.min_value; value <= param.info.max_value; value += 1) {
        if (!plugin_params->value_to_text(plugin, param.info.id, value, text, sizeof(text)))
            return "";
        if (!labels.empty())
            labels += ",";
        labels += json_string(text);
    }
    return labels;
}

static void print_params() {
    for (const param_t& param : params) {
        auto flag = [&](uint32_t mask) { return param.info.flags & mask ? "true" : "false"; };
        print_line("#PRM> {\"id\":" + std::to_string(param.info.id) +
            ",\"name\":" + json_string(param.info.name) +
            ",\"module\":" + json_string(param.info.module) +
            ",\"min\":" + json_number(param.info.min_value) +
            ",\"max\":" + json_number(param.info.max_value) +
            ",\"default\":" + json_number(param.info.default_value) +
            ",\"value\":" + json_number(param.value) +
            ",\"stepped\":" + flag(CLAP_PARAM_IS_STEPPED) +
            ",\"enum\":" + flag(CLAP_PARAM_IS_ENUM) +
            ",\"bypass\":" + flag(CLAP_PARAM_IS_BYPASS) +
            ",\"hidden\":" + flag(CLAP_PARAM_IS_HIDDEN) +
            ",\"readonly\":" + flag(CLAP_PARAM_IS_READONLY) +
            ",\"labels\":[" + get_param_labels(param) + "]}");
    }
}

static void print_param_value(clap_id id, double value) {
    print_line("#CTR> " + std::to_string(id) + "=" + json_number(value));
}

static param_t* find_param(clap_id id) {
    for (param_t& param : params)
        if (param.info.id == id)
            return &param;
    return nullptr;
}

// Report the parameters whose value differs from the cached one
static void print_changed_params() {
    for (param_t& param : params) {
        double value;
        if (!plugin_params->get_value(plugin, param.info.id, &value) || value == param.value)
            continue;
        param.value = value;
        print_param_value(param.info.id, value);
    }
}

// Report the values the plugin changed while processing
static void print_param_feedback() {
    param_msg_t msg;
    while (jack_ringbuffer_read_space(ring_to_main) >= sizeof(msg)) {
        jack_ringbuffer_read(ring_to_main, (char*)&msg, sizeof(msg));
        if (param_t* param = find_param(msg.id))
            param->value = msg.value;
        print_param_value(msg.id, msg.value);
    }
}

static void set_param(clap_id id, double value) {
    param_t* param = find_param(id);
    if (!param) {
        fprintf(stderr, "zynclap: unknown parameter %u\n", id);
        return;
    }
    param_msg_t msg = {id, param->info.cookie, value};
    if (jack_ringbuffer_write_space(ring_to_audio) < sizeof(msg)) {
        fprintf(stderr, "zynclap: parameter queue is full, dropping value for %u\n", id);
        return;
    }
    jack_ringbuffer_write(ring_to_audio, (const char*)&msg, sizeof(msg));
    param->value = value;
}

static int64_t state_write(const clap_ostream_t* stream, const void* buffer, uint64_t size) {
    std::vector<uint8_t>* data = (std::vector<uint8_t>*)stream->ctx;
    const uint8_t* bytes = (const uint8_t*)buffer;
    data->insert(data->end(), bytes, bytes + size);
    return size;
}

struct state_reader_t {
    const std::vector<uint8_t>* data;
    size_t pos;
};

static int64_t state_read(const clap_istream_t* stream, void* buffer, uint64_t size) {
    state_reader_t* reader = (state_reader_t*)stream->ctx;
    uint64_t left = reader->data->size() - reader->pos;
    if (size > left)
        size = left;
    memcpy(buffer, reader->data->data() + reader->pos, size);
    reader->pos += size;
    return size;
}

static void print_state() {
    std::vector<uint8_t> data;
    clap_ostream_t stream = {&data, state_write};
    if (!plugin_state || !plugin_state->save(plugin, &stream))
        data.clear();
    print_line("#STA> " + base64_encode(data));
}

static void load_state(const std::string& encoded) {
    if (!plugin_state)
        return;
    std::vector<uint8_t> data = base64_decode(encoded);
    state_reader_t reader = {&data, 0};
    clap_istream_t stream = {&reader, state_read};
    if (!plugin_state->load(plugin, &stream))
        fprintf(stderr, "zynclap: can't load plugin state\n");
    print_changed_params();
}

// -----------------------------------------------------------------------------
// Commands
// -----------------------------------------------------------------------------

static void run_command(const std::string& line) {
    if (line.rfind("set ", 0) == 0) {
        unsigned int id;
        double value;
        if (sscanf(line.c_str() + 4, "%u %lf", &id, &value) == 2)
            set_param(id, value);
        else
            fprintf(stderr, "zynclap: wrong command '%s'\n", line.c_str());
    } else if (line == "state") {
        print_state();
    } else if (line.rfind("load ", 0) == 0) {
        load_state(line.substr(5));
    } else if (line.rfind("bypass ", 0) == 0) {
        bypassed = atoi(line.c_str() + 7) != 0;
    } else if (line == "params") {
        load_params();
        print_params();
    } else if (!line.empty()) {
        fprintf(stderr, "zynclap: unknown command '%s'\n", line.c_str());
    }
    print_line(">");
}

static void on_signal(int sig) {
    quit = true;
}

static void main_loop() {
    std::string pending;
    char buf[65536];
    struct pollfd pfd = {STDIN_FILENO, POLLIN, 0};
    while (!quit) {
        if (poll(&pfd, 1, MAIN_LOOP_PERIOD_MS) > 0) {
            ssize_t n = read(STDIN_FILENO, buf, sizeof(buf));
            if (n <= 0)
                break;
            pending.append(buf, n);
            size_t end;
            while ((end = pending.find('\n')) != std::string::npos) {
                run_command(pending.substr(0, end));
                pending.erase(0, end + 1);
            }
        }
        print_param_feedback();
        if (callback_requested.exchange(false))
            plugin->on_main_thread(plugin);
        if (rescan_requested.exchange(false) && plugin_params)
            print_changed_params();
        if (restart_requested.exchange(false))
            restart_plugin();
    }
}

// -----------------------------------------------------------------------------
// Run mode
// -----------------------------------------------------------------------------

static int run(const char* jackname, const char* path, const char* plugin_id) {
    main_thread = pthread_self();

    const clap_plugin_factory_t* factory = load_plugin_file(path);
    if (!factory) {
        fprintf(stderr, "zynclap: no plugin factory in '%s'\n", path);
        return 1;
    }
    plugin = factory->create_plugin(factory, &host, plugin_id);
    if (!plugin || !plugin->init(plugin)) {
        fprintf(stderr, "zynclap: can't create plugin '%s'\n", plugin_id);
        return 1;
    }
    plugin_params = (const clap_plugin_params_t*)plugin->get_extension(plugin, CLAP_EXT_PARAMS);
    plugin_state = (const clap_plugin_state_t*)plugin->get_extension(plugin, CLAP_EXT_STATE);

    jack_client = jack_client_open(jackname, JackNoStartServer, nullptr);
    if (!jack_client) {
        fprintf(stderr, "zynclap: can't connect to JACK\n");
        return 1;
    }
    if (!register_audio_ports(true) || !register_audio_ports(false) || !register_midi_ports()) {
        fprintf(stderr, "zynclap: can't register JACK ports\n");
        return 1;
    }

    ring_to_audio = jack_ringbuffer_create(RING_MESSAGES * sizeof(param_msg_t));
    ring_to_main = jack_ringbuffer_create(RING_MESSAGES * sizeof(param_msg_t));

    jack_set_process_callback(jack_client, on_jack_process, nullptr);
    jack_set_buffer_size_callback(jack_client, on_jack_config_change, nullptr);
    jack_set_sample_rate_callback(jack_client, on_jack_config_change, nullptr);
    jack_on_shutdown(jack_client, on_jack_shutdown, nullptr);

    if (!activate_plugin())
        return 1;
    if (jack_activate(jack_client)) {
        fprintf(stderr, "zynclap: can't activate JACK client\n");
        return 1;
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);

    print_line(std::string("JACK Name: ") + jack_get_client_name(jack_client));
    load_params();
    print_params();
    print_line(">");

    main_loop();

    pause_processing();
    jack_deactivate(jack_client);
    if (active)
        plugin->deactivate(plugin);
    plugin->destroy(plugin);
    entry->deinit();
    jack_client_close(jack_client);
    return 0;
}

static int usage() {
    fprintf(stderr,
        "Usage: zynclap -n <jackname> <plugin file> <plugin id>\n"
        "       zynclap --scan\n");
    return 1;
}

int main(int argc, char** argv) {
    if (argc == 2 && !strcmp(argv[1], "--scan"))
        return scan();
    if (argc == 5 && !strcmp(argv[1], "-n"))
        return run(argv[2], argv[3], argv[4]);
    return usage();
}
