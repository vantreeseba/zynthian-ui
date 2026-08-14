/*
 * ******************************************************************
 * ZYNTHIAN PROJECT: zynclippy Library
 *
 * Library providing sample clip launcher as a Jack connected device
 *
 * Copyright (C) 2025 Brian Walton <brian@riban.co.uk>
 *                    Fernando Moyano <jofemodo@zynthian.org>
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

#include "clippy.h"
#include <unistd.h> // provides usleep
#include <string.h> // provides memset, memcpy, strcpy
#include <jack/jack.h> // provides jack API
#include <jack/midiport.h> // provides jack midi port API
#include <sndfile.h>   // provides sound file manipulation
#include <samplerate.h> // provides samplerate convertor
#include <rubberband/rubberband-c.h> // provides time stretch
#include <math.h> // provides pow for dB calcs


#define FX_NONE 0
#define FX_FADE_IN 1
#define FX_FADE_OUT 2

#define REC_FADE_FRAMES 128 // Seam de-click fade length for recorded clips (~2.7ms @ 48kHz)

typedef struct {
    uint8_t state;          // Clip state
    uint32_t frames;        // Quantity of frames in loaded clip
    uint8_t channels;       // Quantity of channels in clip
    float gain;             // Gain factor
    uint16_t nbeats;        // Number of beats
    uint32_t start;         // Start frame in sample file
    uint32_t end;           // End frame in sample file
    uint8_t quality;        // Re-sample quality
    float tempo;            // Tempo to play (used to calculate timestretch ratio). 0 to no timestretch.
    uint8_t tempo_lock;     // Ignore global tempo changes
    char path[256];         // Loaded file path and filename
    float *data[2];         // Processed sample data for each channel (L,R) - may be offset into alloc[]
    float *alloc[2];        // Base pointers of the allocations backing data[] (used for free)
} Clip;

typedef struct {
    uint8_t state;           // Play state
    uint32_t play_pos;       // Position of playhead in frames
    uint32_t start_frame;    // Starting position in frames => note-on event time
    uint32_t beat;           // Beat counter
    jack_port_t* jack_out_a; // Left jack output port
    jack_port_t* jack_out_b; // Right jack output port
    jack_port_t* jack_in_a;  // Left jack capture input port
    jack_port_t* jack_in_b;  // Right jack capture input port
    SNDFILE* sndfile;        // Pointer to an open sndfile used to read current clip data
    Clip* clips[MAX_CLIPS];  // Array of pointers to clip objects
    Clip* current_clip;      // Pointer to the currently playing clip
    int current_clip_id;     // Index of currently playing clip
    Clip* starting_clip;     // Pointer to the starting clip
    int starting_clip_id;    // Index of starting clip
    volatile uint8_t monitor; // 1 to mix capture input ports into the player output (live input monitoring)
} Player;

typedef union {
    uint32_t u32;
    struct {
        uint8_t value2;
        uint8_t value1;
        uint8_t command;
        uint8_t unused;
    };
} MidiMsg;

// Global variables
jack_nframes_t samplerate = 48000;
jack_nframes_t buffersize = 1024;
static jack_port_t* midi_input_port;
static jack_client_t* jack_client;
static volatile uint8_t mutex = 0;
Player* players[16]; // Up to 16 players, 1 per MIDI channel

typedef struct {
    volatile uint8_t state; // Record state (REC_STATE)
    uint8_t channel;        // MIDI channel being recorded
    uint8_t clip_id;        // Clip index (note - 1) being recorded
    uint8_t channels;       // Quantity of channels to capture (1 or 2)
    uint32_t max_frames;    // Capture buffer capacity in frames
    uint32_t frames;        // Quantity of frames captured so far
    uint16_t beats;         // Quantity of beat ticks counted whilst recording
    float tempo;            // Tempo (BPM) when armed
    uint32_t latency;       // Capture latency (frames) to compensate, sampled at punch-in
    uint32_t loop_frames;   // Committed loop length in frames (set at punch-out)
    uint32_t target_frames; // Total frames to capture (loop + latency tail) before REC_DONE
    float* data[2];         // Capture buffers, handed over to pending_clip at commit
    float* tail[2];         // Aliases of the committed clip's alloc[] whilst capturing the latency tail (not owned)
    Clip* pending_clip;     // Preallocated clip shell committed at punch-out (RT-safe pointer swap)
    Clip* old_clip;         // Clip displaced at commit, freed later by disarmRecord()
    Clip* committed_clip;   // Clip committed at punch-out, shrink-wrapped by disarmRecord() (not owned)
} Recorder;

static Recorder g_recorder = {REC_IDLE}; // Single global recorder => only one clip records at a time

// User latency offset (frames) added to the JACK-reported capture latency when recording clips
static volatile int32_t g_record_latency_offset = 0;

// True to clear the player's live input monitor in the RT commit path at punch-out
// (AUTO monitor mode => avoids the committed loop doubling the still-open monitor)
static volatile uint8_t g_record_monitor_auto = 0;

// Live input monitor routing: 0 => mix into each player's output (feeds the mixbus),
// 1 => mix into the dedicated monitor_a/b ports (direct hardware monitoring)
static jack_port_t* monitor_port_a = NULL;
static jack_port_t* monitor_port_b = NULL;
static volatile uint8_t monitor_direct = 0;
static volatile float monitor_gain = 1.0f; // Gain applied on the direct monitor ports only

static void inline getMutex() {
    while (mutex)
        usleep(100);
    mutex = 1;
}

static void inline releaseMutex() {
    mutex = 0;
}

float* out_buff_a[16];
float* out_buff_b[16];

// Bake a short linear fade into a recorded clip's head (fade-in) or tail (fade-out).
// A recorded loop almost never starts/ends on a zero crossing => the end->start
// seam clicks on every repeat. Applied once, so the saved WAV loops cleanly too.
static void applyRecordedFade(Clip* clip, uint8_t tail) {
    uint32_t fade = REC_FADE_FRAMES;
    if (fade > clip->frames / 2)
        fade = clip->frames / 2;
    for (int ch = 0; ch < clip->channels; ++ch) {
        float* data = clip->data[ch];
        if (tail) {
            for (uint32_t i = 0; i < fade; ++i)
                data[clip->frames - 1 - i] *= (float)i / fade;
        } else {
            for (uint32_t i = 0; i < fade; ++i)
                data[i] *= (float)i / fade;
        }
    }
}

jack_nframes_t process_clip(uint8_t channel, Clip* clip, jack_nframes_t frames, int32_t pos, uint8_t fx) {
    jack_nframes_t offset = 0;
    // Playing offset => Starting => Fade-in
    if (pos < 0) {
        offset = -pos;
        frames -= offset;
        pos = 0;
    }
    // Out of range
    if (pos >= clip->frames)
        return 0;
    // Last fragment. Compare this way round: clip->frames - frames would
    // wrap for clips shorter than one period (possible for recorded takes),
    // skipping the clamp and reading past the clip buffer.
    if (frames > clip->frames - (uint32_t)pos)
        frames = clip->frames - pos;

    float gain;
    float dGain = clip->gain / frames;
    for (jack_nframes_t i = 0; i < frames; i++) {
        switch (fx) {
            case FX_NONE:
                gain = clip->gain;
                break;
            case FX_FADE_IN:
                gain = i * dGain;
                break;
            case FX_FADE_OUT:
                gain = clip->gain - i * dGain;
                break;
        }
        out_buff_a[channel][offset + i] += clip->data[0][pos + i] * gain;
        out_buff_b[channel][offset + i] += clip->data[1][pos + i] * gain;
    }

    //printf("PROCESSING CLIP %u AT %u WITH FX %u (%f => %f) => %d -> %u (%u)\n", channel, offset, fx, clip->gain, gain, pos, pos + frames, clip->frames);

    return frames;
}

static int process(jack_nframes_t frames, __attribute__((unused)) void* arg) {
    Player* player;

    while (mutex)
        usleep(10);
    mutex = 1;

    void* midi_buffer = jack_port_get_buffer(midi_input_port, frames);
    jack_nframes_t numMidiEvents = jack_midi_get_event_count(midi_buffer);
    jack_midi_event_t event;

    // Clip capture bookkeeping for this cycle
    uint8_t rec_commit = 0;             // Punch-out received this cycle
    jack_nframes_t rec_from = 0;        // First frame to capture (punch-in offset)
    jack_nframes_t rec_to = frames;     // Frame after last frame to capture (punch-out offset)

    // Received MIDI messages
    for (uint32_t i = 0; i < numMidiEvents; ++i) {
        if (jack_midi_event_get(&event, midi_buffer, i) != 0)
            continue;

        if (event.size == 0)
            continue;

        switch (event.buffer[0] & 0xf0) {
            case MIDI_NOTE_ON:
                if (event.buffer[2] != 0) {
                    // Note on triggers playback from preload buffer
                    uint8_t channel = event.buffer[0] & 0x0f;
                    player = players[channel];
                    if (!player)
                        break;
                    if (event.buffer[2] >= CLIPPY_VEL_REC_START && event.buffer[2] <= CLIPPY_VEL_REC_ABORT) {
                        // Record punch messages only act on the armed recorder target
                        uint8_t clip_id = event.buffer[1] - 1;
                        switch (event.buffer[2]) {
                            case CLIPPY_VEL_REC_START:
                                if (g_recorder.state == REC_ARMED && channel == g_recorder.channel && clip_id == g_recorder.clip_id) {
                                    g_recorder.frames = 0;
                                    g_recorder.beats = 0;
                                    g_recorder.loop_frames = 0;
                                    // Captured audio arrives late by the (JACK-reported) capture latency plus
                                    // any user offset => compensate by shifting the committed loop region
                                    int32_t latency = g_record_latency_offset;
                                    if (player->jack_in_a) {
                                        jack_latency_range_t range;
                                        jack_port_get_latency_range(player->jack_in_a, JackCaptureLatency, &range);
                                        latency += (int32_t)range.max;
                                    }
                                    if (latency < 0)
                                        latency = 0;
                                    else if ((uint32_t)latency > samplerate)
                                        latency = samplerate; // Sanity cap: 1s
                                    g_recorder.latency = (uint32_t)latency;
                                    g_recorder.state = REC_RECORDING;
                                    rec_from = event.time;
                                    // Recording replaces any playing clip => fade it out this cycle
                                    if (player->state == STATE_PLAYING)
                                        player->state = STATE_STOPPING;
                                }
                                break;
                            case CLIPPY_VEL_REC_STOP:
                                if (g_recorder.state == REC_RECORDING && !rec_commit && channel == g_recorder.channel && clip_id == g_recorder.clip_id) {
                                    rec_to = event.time;
                                    rec_commit = 1;
                                    // Coincident beat tick (emitted after this message) increments to 1, matching a normal clip start
                                    player->beat = 0;
                                }
                                break;
                            case CLIPPY_VEL_REC_ABORT:
                                if ((g_recorder.state == REC_ARMED || g_recorder.state == REC_RECORDING) && channel == g_recorder.channel)
                                    g_recorder.state = REC_ABORTED;
                                break;
                        }
                        break;
                    }
                    if (event.buffer[1] == 0) {
                        // Note 0 stops playback
                        if (player->state == STATE_PLAYING) {
                            player->state = STATE_STOPPING;
                        } else {
                            player->state = STATE_READY;
                        }
                    } else {
                        if (player->state == STATE_READY || player->state == STATE_PLAYING) {
                            // Set starting clip
                            uint8_t clip_id = event.buffer[1] - 1;
                            if (clip_id < MAX_CLIPS) {
                                // If the clip is not null
                                if (player->clips[clip_id]) {
                                    player->starting_clip = player->clips[clip_id];
                                    player->starting_clip_id = clip_id;
                                    player->start_frame = event.time;
                                    player->beat = 0;
                                    player->state = STATE_PLAYING;
                                }
                                // else if the clip is NULL => STOP
                                else {
                                    if (player->state == STATE_PLAYING) {
                                        player->state = STATE_STOPPING;
                                    } else {
                                        player->state = STATE_READY;
                                    }
                                }
                            }
                        }
                    }
                    break;
                }
                [[fallthrough]];
            case MIDI_NOTE_OFF:
                // Not handling note-off
                break;
            case MIDI_CC:
                //setGain(event.buffer[0] & 0x0f, event.buffer[1], (float)(event.buffer[2]) / 64);
                break;
            case MIDI_AFTERTOUCH:
                // Used for beat sync
                uint8_t channel = event.buffer[0] & 0x0f;
                // Count beats whilst capturing (a tick coincident with punch-out arrives after the punch message so is not counted)
                if (g_recorder.state == REC_RECORDING && !rec_commit && channel == g_recorder.channel)
                    g_recorder.beats++;
                player = players[channel];
                if (!player)
                    break;
                // Resume playing at beat sync
                if (player->state == STATE_SYNCYNG && player->current_clip) {
                    // Sync to start position
                    player->beat = player->beat % player->current_clip->nbeats;
                    if (player->beat == 0) {
                        player->starting_clip = player->current_clip;
                        player->starting_clip_id = player->current_clip_id;
                        player->start_frame = event.time;
                    }
                    // Sync to beat position
                    else {
                        player->play_pos = (player->beat * player->current_clip->frames / player->current_clip->nbeats) - event.time;
                    }
                    player->state = STATE_PLAYING;
                    //printf("SYNCYNG AT => %d / %d (NUM BEATS = %d)\n", player->play_pos, player->current_clip->frames, player->current_clip->nbeats);
                }
                player->beat++;
                //printf("Beat => %d\n", player->beat);
                break;
        }
    }

    // Capture audio into record buffer
    if (g_recorder.state == REC_RECORDING) {
        Player* rp = players[g_recorder.channel];
        jack_nframes_t n = (rec_to > rec_from) ? (rec_to - rec_from) : 0;
        jack_nframes_t avail = (g_recorder.max_frames > g_recorder.frames) ? (g_recorder.max_frames - g_recorder.frames) : 0;
        if (n > avail) {
            // Capture buffer exhausted => abandon recording (safety net - zynseq's bar cap should punch out first)
            g_recorder.state = REC_OVERFLOW;
        } else if (rp && rp->jack_in_a && rp->jack_in_b) {
            if (n) {
                float* in = jack_port_get_buffer(rp->jack_in_a, frames);
                memcpy(g_recorder.data[0] + g_recorder.frames, in + rec_from, n * sizeof(float));
                if (g_recorder.channels == 2) {
                    in = jack_port_get_buffer(rp->jack_in_b, frames);
                    memcpy(g_recorder.data[1] + g_recorder.frames, in + rec_from, n * sizeof(float));
                }
                g_recorder.frames += n;
            }
            if (rec_commit) {
                // Latency compensation: the wanted loop content is captured at
                // indices [latency, loop + latency) => offset the clip's data
                // pointers and keep capturing the tail after punch-out
                uint32_t latency = g_recorder.latency;
                if (latency > g_recorder.frames)
                    latency = g_recorder.frames;
                if (g_recorder.frames + latency > g_recorder.max_frames)
                    latency = g_recorder.max_frames - g_recorder.frames;
                // Commit recording as the clip and start looping it (pointer swaps only)
                Clip* clip = g_recorder.pending_clip;
                g_recorder.loop_frames = g_recorder.frames;
                clip->frames = g_recorder.loop_frames;
                clip->nbeats = g_recorder.beats ? g_recorder.beats : 1;
                clip->channels = g_recorder.channels;
                clip->tempo = g_recorder.tempo;
                clip->start = 0;
                clip->end = clip->frames;
                clip->alloc[0] = g_recorder.data[0];
                clip->alloc[1] = (g_recorder.channels == 2) ? g_recorder.data[1] : NULL;
                clip->data[0] = g_recorder.data[0] + latency;
                clip->data[1] = (g_recorder.channels == 2) ? (g_recorder.data[1] + latency) : clip->data[0];
                // De-click the loop seam: fade in the head now; the tail fade is
                // applied once its (latency-delayed) content is complete
                applyRecordedFade(clip, 0);
                clip->state = STATE_READY;
                g_recorder.old_clip = rp->clips[g_recorder.clip_id];
                if (rp->current_clip == g_recorder.old_clip) {
                    rp->current_clip = NULL;
                    rp->current_clip_id = -1;
                }
                rp->clips[g_recorder.clip_id] = clip;
                // Seamless record => loop: start playback of the new clip at the punch-out frame
                // (playback section below consumes starting_clip and sets STATE_PLAYING)
                rp->starting_clip = clip;
                rp->starting_clip_id = g_recorder.clip_id;
                rp->start_frame = rec_to;
                if (g_record_monitor_auto)
                    // AUTO monitor => mute the live input the moment the loop takes over,
                    // otherwise it doubles the playback until the UI catches up
                    rp->monitor = 0;
                g_recorder.pending_clip = NULL;
                g_recorder.committed_clip = clip;
                if (latency) {
                    // Keep capturing until the loop's tail (delayed by the
                    // latency) has fully arrived. The buffer is now owned by
                    // the clip; tail[] alias it and are never freed.
                    // The tail writer stays a full loop-length ahead of the
                    // playhead (which starts at the loop start) => no race.
                    g_recorder.target_frames = g_recorder.loop_frames + latency;
                    g_recorder.tail[0] = g_recorder.data[0];
                    g_recorder.tail[1] = g_recorder.data[1];
                    g_recorder.state = REC_FINISHING;
                } else {
                    applyRecordedFade(clip, 1);
                    g_recorder.state = REC_DONE;
                }
                g_recorder.data[0] = NULL;
                g_recorder.data[1] = NULL;
            }
        }
    }

    // Capture the post-punch-out latency tail into the committed clip's buffer
    if (g_recorder.state == REC_FINISHING) {
        Player* rp = players[g_recorder.channel];
        if (rp && rp->jack_in_a && rp->jack_in_b) {
            jack_nframes_t tail_from = rec_commit ? rec_to : 0; // Commit cycle => tail starts at the punch-out frame
            jack_nframes_t need = g_recorder.target_frames - g_recorder.frames;
            jack_nframes_t n = (frames > tail_from) ? (frames - tail_from) : 0;
            if (n > need)
                n = need;
            if (n) {
                float* in = jack_port_get_buffer(rp->jack_in_a, frames);
                memcpy(g_recorder.tail[0] + g_recorder.frames, in + tail_from, n * sizeof(float));
                if (g_recorder.channels == 2) {
                    in = jack_port_get_buffer(rp->jack_in_b, frames);
                    memcpy(g_recorder.tail[1] + g_recorder.frames, in + tail_from, n * sizeof(float));
                }
                g_recorder.frames += n;
            }
        } else {
            // Capture ports vanished => give up on the (calloc'd, silent) remainder of the tail
            g_recorder.frames = g_recorder.target_frames;
        }
        if (g_recorder.frames >= g_recorder.target_frames) {
            // Tail content complete => de-click the loop seam end
            // (the playhead is only ~latency frames in => nowhere near the tail)
            if (rp) {
                Clip* clip = rp->clips[g_recorder.clip_id];
                if (clip)
                    applyRecordedFade(clip, 1);
            }
            g_recorder.tail[0] = NULL;
            g_recorder.tail[1] = NULL;
            g_recorder.state = REC_DONE;
        }
    }

    // Clear the direct monitor output buffers (accumulated with += below)
    float* mon_buff_a = NULL;
    float* mon_buff_b = NULL;
    if (monitor_port_a && monitor_port_b) {
        mon_buff_a = jack_port_get_buffer(monitor_port_a, frames);
        mon_buff_b = jack_port_get_buffer(monitor_port_b, frames);
        memset(mon_buff_a, 0, frames * sizeof(float));
        memset(mon_buff_b, 0, frames * sizeof(float));
    }

    // Populate player audio output buffers from sample data buffers
    for (uint8_t channel = 0; channel < 16; channel++) {
        player = players[channel];
        if (!player) continue;
        out_buff_a[channel] = jack_port_get_buffer(player->jack_out_a, frames);
        out_buff_b[channel] = jack_port_get_buffer(player->jack_out_b, frames);
        memset(out_buff_a[channel], 0, frames * sizeof(float));
        memset(out_buff_b[channel], 0, frames * sizeof(float));

        // Live input monitoring: pass capture input through to the player output
        // (before clip mixing which accumulates with += and may continue early)
        // or, in direct mode, to the dedicated monitor ports with the monitor gain
        if (player->monitor && player->jack_in_a && player->jack_in_b) {
            float* in_a = jack_port_get_buffer(player->jack_in_a, frames);
            float* in_b = jack_port_get_buffer(player->jack_in_b, frames);
            if (monitor_direct && mon_buff_a) {
                float gain = monitor_gain;
                for (jack_nframes_t i = 0; i < frames; ++i) {
                    mon_buff_a[i] += in_a[i] * gain;
                    mon_buff_b[i] += in_b[i] * gain;
                }
            } else {
                for (jack_nframes_t i = 0; i < frames; ++i) {
                    out_buff_a[channel][i] += in_a[i];
                    out_buff_b[channel][i] += in_b[i];
                }
            }
        }

        // New clip starting => Cross-fade exiting and starting clips
        if (player->starting_clip) {
            // Stop current clip if any => fade-out
            if (player->current_clip && player->state == STATE_PLAYING)
                process_clip(channel, player->current_clip, frames, player->play_pos, FX_FADE_OUT);
            // Set current clip
            player->current_clip = player->starting_clip;
            player->current_clip_id = player->starting_clip_id;
            player->starting_clip = NULL;
            // Start new clip => fade-in
            player->play_pos = process_clip(channel, player->current_clip, frames, -player->start_frame, FX_FADE_IN);
            player->state = STATE_PLAYING;
        }
        // Clip playing or stopping
        else if (player->current_clip) {
            uint8_t fx;
            switch (player->state) {
                case STATE_PLAYING:
                    fx = FX_NONE;
                    break;
                case STATE_STOPPING:
                    player->state = STATE_READY;
                    fx = FX_FADE_OUT;
                    break;
                case STATE_IDLE:
                case STATE_LOAD:
                case STATE_READY:
                case STATE_SYNCYNG:
                case STATE_STARTING:
                    continue;
            }
            jack_nframes_t dpos = process_clip(channel, player->current_clip, frames, player->play_pos, fx);
            if (dpos > 0)
                player->play_pos += dpos;
            else
                player->state = STATE_READY;
        }
    }
    mutex = 0;
    return 0;
}

void reset() {
    // Recording cannot survive a samplerate change => abandon it
    if (g_recorder.state != REC_IDLE)
        disarmRecord();
    getMutex();
    for (uint8_t ch = 0; ch < 16; ch++) {
        Player* player = players[ch];
        if (!player)
            continue;
        player->state=STATE_LOAD;
        for (uint8_t id = 0; id < MAX_CLIPS; ++id) {
            Clip* clip = player->clips[id];
            // Skip clips not yet saved to file (recorded, empty path) => cannot be reloaded
            if (clip && clip->path[0]) {
                releaseMutex();
                loadClip(ch, id + 1, clip->path, clip->nbeats, clip->start,
                         clip->end, clip->quality, clip->tempo, clip->tempo_lock);
                getMutex();
            }
        }
        player->state=STATE_READY;
    }
    releaseMutex();
}

void changeTempo(float tempo) {
    // Start by playing ones
    int ids[16];
    for (uint8_t ch = 0; ch < 16; ch++) {
        Player* player = players[ch];
        if (!player) {
            ids[ch] = -1;
            continue;
        }
        if (player->current_clip_id >= 0) {
            ids[ch] = player->current_clip_id;
        } else {
            ids[ch] = 0;
        }
    }
    // Reload all clips, recalculating timestretch with new tempo
    for (uint8_t i = 0; i < MAX_CLIPS; i++) {
        for (uint8_t ch = 0; ch < 16; ch++) {
            Player* player = players[ch];
            if (!player || ids[ch] < 0)
                continue;
            uint8_t id = (ids[ch] + i) % MAX_CLIPS;
            Clip* clip = player->clips[id];
            // Don't process clips with tempo=0 (no timestretch), locked tempo or no file (recorded, unsaved).
            if (clip && clip->path[0] && !clip->tempo_lock && clip->tempo > 0 && tempo != clip->tempo) {
                loadClip(ch, id + 1, clip->path, clip->nbeats, clip->start, clip->end, clip->quality, tempo, clip->tempo_lock);
            }
        }
    }
}

void idlePlayers() {
    for (uint8_t ch = 0; ch < 16; ch++) {
        Player* player = players[ch];
        if (!player || !player->current_clip || player->state != STATE_PLAYING) continue;
        if (!player->current_clip->tempo_lock && player->current_clip->tempo > 0) {
            getMutex();
            player->state = STATE_IDLE;
            releaseMutex();
        }
    }
}

void changeClipTempo(uint8_t channel, uint8_t id, float tempo) {
    if (channel > 16 || id >= MAX_CLIPS) return;
    Player* player = players[channel];
    if (!player) return;
    Clip* clip = player->clips[id];
    if (clip && clip->path[0] && tempo != clip->tempo) {
        // Reload clip, recalculating timestretch with new tempo
        loadClip(channel, id + 1, clip->path, clip->nbeats, clip->start, clip->end, clip->quality, tempo, clip->tempo_lock);
    }
}

void setClipTempoLock(uint8_t channel, uint8_t id, uint8_t tempo_lock) {
    if (channel > 16 || id >= MAX_CLIPS) return;
    Player* player = players[channel];
    if (!player) return;
    Clip* clip = player->clips[id];
    if (clip) clip->tempo_lock = tempo_lock;
}

void setClipBeats(uint8_t channel, uint8_t id, uint16_t nbeats, float tempo) {
    if (channel > 16 || id >= MAX_CLIPS) return;
    Player* player = players[channel];
    if (!player) return;
    Clip* clip = player->clips[id];
    if (clip && nbeats) {
        clip->nbeats = nbeats;
        clip->tempo = tempo;
    }
}

static int onBufferSize(jack_nframes_t frames, __attribute__((unused)) void* arg) {
    buffersize = frames;
    //reset();
    return 0;
}

static int onSamplerate(jack_nframes_t frames, __attribute__((unused)) void* arg) {
    samplerate = frames;
    reset();
    return 0;
}

void end() {
    disarmRecord();
    for (uint8_t i = 0; i < 16; ++i)
        removePlayer(i);
    if (jack_client)
        jack_client_close(jack_client);
    jack_client = NULL;
}

/** @brief  Initialise the library
    @param  jackname Requested jack client name
    @retval int Error code
*/
int init() {
    int error = ERROR_SUCCESS;
    if (jack_client)
        return ERROR_EXISTS;
    // Register the cleanup function to be called when library exits
    atexit(end);

    // Initialise players
    for (uint8_t i = 0; i < 16; ++i)
        players[i] = NULL;

    // Create jack client
    jack_status_t status;
    jack_client = jack_client_open("clippy", JackNullOption, &status);
    if (jack_client == NULL) {
        fprintf(stderr, "Could not open JACK client\n");
        end();
        return ERROR_CREATE;
    }

    midi_input_port = jack_port_register(jack_client, "in", JACK_DEFAULT_MIDI_TYPE, JackPortIsInput, 0);
    if (midi_input_port == NULL) {
        fprintf(stderr, "Could not open MIDI input port\n");
        end();
        return ERROR_PORT;
    }

    monitor_port_a = jack_port_register(jack_client, "monitor_a", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    monitor_port_b = jack_port_register(jack_client, "monitor_b", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    if (monitor_port_a == NULL || monitor_port_b == NULL) {
        fprintf(stderr, "Could not open monitor output ports\n");
        end();
        return ERROR_PORT;
    }

    jack_set_sample_rate_callback(jack_client, onSamplerate, NULL);
    jack_set_buffer_size_callback(jack_client, onBufferSize, NULL);
    jack_set_process_callback(jack_client, process, NULL);

    if (jack_activate(jack_client) != 0) {
        fprintf(stderr, "Could not activate client\n");
        end();
        return ERROR_ACTIVATE;
    }
    return ERROR_SUCCESS;
}

/** @brief  Get the jack client name
    @retval const char* Jack name
*/
const char* getJackname() {
    return jack_get_client_name(jack_client);
}

uint8_t addPlayer(uint8_t channel) {
    if (channel >= 16) {
        for (channel = 0; channel < 16; ++channel) {
            if (players[channel] == NULL)
                break;
        }
    }
    if (channel > 16)
        return 255;
    Player* player = malloc(sizeof(Player));
    if (!player)
        return ERROR_CREATE;
    memset(player, 0, sizeof(Player));
    char name[16];
    sprintf(name, "out_%02ua", channel + 1);
    player->jack_out_a = jack_port_register(jack_client, name, JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    sprintf(name, "out_%02ub", channel + 1);
    player->jack_out_b = jack_port_register(jack_client, name, JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    if (player->jack_out_a == NULL || player->jack_out_b == NULL)
        fprintf(stderr, "Clippy error: failed to create jack output ports\n");
    sprintf(name, "input_%02ua", channel + 1);
    player->jack_in_a = jack_port_register(jack_client, name, JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, 0);
    sprintf(name, "input_%02ub", channel + 1);
    player->jack_in_b = jack_port_register(jack_client, name, JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, 0);
    if (player->jack_in_a == NULL || player->jack_in_b == NULL)
        fprintf(stderr, "Clippy error: failed to create jack input ports\n");
    for (uint32_t id = 0; id < MAX_CLIPS; ++id)
        player->clips[id] = NULL;
    player->current_clip_id = -1;
    player->state = STATE_READY;
    getMutex();
    players[channel] = player;
    releaseMutex();
    return channel;
}

uint8_t removePlayer(uint8_t channel) {
    if(channel >= 16)
        return ERROR_RANGE;
    Player* player = players[channel];
    if(player == NULL)
        return ERROR_CREATE;
    // Abort any recording targeting this player
    if ((g_recorder.state == REC_ARMED || g_recorder.state == REC_RECORDING) && g_recorder.channel == channel) {
        getMutex();
        g_recorder.state = REC_ABORTED;
        releaseMutex();
    }
    // Stop any latency tail capture before its clip buffer is freed below
    if (g_recorder.state == REC_FINISHING && g_recorder.channel == channel) {
        getMutex();
        g_recorder.tail[0] = NULL;
        g_recorder.tail[1] = NULL;
        g_recorder.state = REC_DONE;
        releaseMutex();
    }
    getMutex();
    players[channel] = NULL;
    releaseMutex();
    for (uint32_t id = 0; id < MAX_CLIPS; ++id) {
        if (player->clips[id]) {
            if (player->clips[id] == g_recorder.committed_clip)
                g_recorder.committed_clip = NULL;
            for (int i=0; i < player->clips[id]->channels; i++)
                free(player->clips[id]->alloc[i]);
            free(player->clips[id]);
        }
    }
    jack_port_unregister(jack_client, player->jack_out_a);
    jack_port_unregister(jack_client, player->jack_out_b);
    jack_port_unregister(jack_client, player->jack_in_a);
    jack_port_unregister(jack_client, player->jack_in_b);
    free(player);
    return ERROR_SUCCESS;
}

uint8_t idlePlayerClip(uint8_t channel, uint8_t clip) {
    if (channel >= 16 || clip >= MAX_CLIPS)
        return ERROR_RANGE;
    Player* player = players[channel];
    if(player == NULL)
        return ERROR_CREATE;
    if (player->current_clip_id == clip && player->state == STATE_PLAYING) {
        getMutex();
        player->state = STATE_IDLE;
        releaseMutex();
    }
    return ERROR_SUCCESS;
}

uint8_t nudgeClip(uint8_t channel, uint8_t clip, uint8_t forward) {
    int nDiff = forward ? 1 : -1;
    int clip2 = clip + nDiff;
    if (clip >= MAX_CLIPS || clip2 >= MAX_CLIPS || channel > 15)
        return ERROR_RANGE;
    Player* pPlayer = players[channel];
    if (!pPlayer)
        return ERROR_RANGE;
    Clip* pClip = pPlayer->clips[clip];
    Clip* pClip2 = pPlayer->clips[clip2];
    getMutex(); //!@todo Check this won't leave stuck notes
    pPlayer->clips[clip] = pClip2;
    pPlayer->clips[clip2] = pClip;
    releaseMutex();
    return ERROR_SUCCESS;
}

uint8_t insertClip(uint8_t channel, uint8_t clip) {
    if (channel > 15 || clip >= MAX_CLIPS)
        return ERROR_RANGE;
    Player* pPlayer = players[channel];
    if (!pPlayer)
        return ERROR_RANGE;
    if (pPlayer->clips[MAX_CLIPS - 1])
        return ERROR_EXISTS;
    for (uint8_t i = MAX_CLIPS - 1; i > clip ; --i) {
        pPlayer->clips[i] = pPlayer->clips[i - 1];
    }
    pPlayer->clips[clip] = NULL;
    return ERROR_SUCCESS;
}

uint8_t removeClip(uint8_t channel, uint8_t clip) {
    if (channel > 15 || clip >= MAX_CLIPS)
        return ERROR_RANGE;
    Player* pPlayer = players[channel];
    if (!pPlayer)
        return ERROR_RANGE;
    unloadClip(channel, clip + 1);
    for (uint8_t i = clip; i < MAX_CLIPS - 1; ++i) {
        pPlayer->clips[i] = pPlayer->clips[i + 1];
    }
    pPlayer->clips[MAX_CLIPS - 1] = NULL;
    pPlayer->state = STATE_READY;
    return ERROR_SUCCESS;
}

uint8_t getFreeClip(uint8_t channel) {
    if (channel >= 16)
        return 0;
    Player* player = players[channel];
    if (!player)
        return 0;
    for (uint8_t id = 0; id < MAX_CLIPS; ++id) {
        if (player->clips[id] == NULL)
            return id + 1;
    }
    return 0;
}

const char * getClipPath(uint8_t channel, uint8_t clip) {
    if (channel >= 16 || clip >= MAX_CLIPS)
        return NULL;
    Player* player = players[channel];
    if (!player)
        return NULL;
    if (player->clips[clip] == NULL)
        return NULL;
    return player->clips[clip]->path;
}

uint32_t getClipFrames(uint8_t channel, uint8_t clip) {
    if (channel >= 16 || clip >= MAX_CLIPS)
        return 0;
    Player* player = players[channel];
    if (!player)
        return 0;
    if (player->clips[clip] == NULL)
        return 0;
    return player->clips[clip]->frames;
}

float getClipTempo(uint8_t channel, uint8_t clip) {
    if (channel >= 16 || clip >= MAX_CLIPS)
        return 0;
    Player* player = players[channel];
    if (!player)
        return 0;
    if (player->clips[clip] == NULL)
        return 0;
    return player->clips[clip]->tempo;
}

uint16_t getClipBeats(uint8_t channel, uint8_t clip) {
    if (channel >= 16 || clip >= MAX_CLIPS)
        return 0;
    Player* player = players[channel];
    if (!player)
        return 0;
    if (player->clips[clip] == NULL)
        return 0;
    return player->clips[clip]->nbeats;
}

uint8_t loadClip(uint8_t channel, uint8_t note, const char* path, uint16_t nbeats,
                 uint32_t start, uint32_t end, uint8_t quality, float tempo,
                 uint8_t tempo_lock) {

    if (channel >= 16) {
        fprintf(stderr,"loadClip(): Channel/note out of range.\n");
        return 0;
    }
    Player* player = players[channel];
    if (!player) {
        fprintf(stderr,"loadClip(): No player in channel %d.\n", channel);
        return 0;
    }

    if (note == 0) {
        // Find next available note
        for (note = 0; note < MAX_CLIPS; ++ note) {
            if (!player->clips[note])
                break;
        }
        note++;
    }
    if (note >= MAX_CLIPS) {
        fprintf(stderr,"loadClip(): Note %d out of range.\n", note);
        return 0;
    }

    uint8_t error = 0;

    // --------------------------------------------------------------
    // Read data and deinterleave
    // --------------------------------------------------------------

    // Read source file into interleaved float buffer data_in
    SF_INFO sf_info;
    memset(&sf_info, 0, sizeof(sf_info));
    SNDFILE* sndfile = sf_open(path, SFM_READ, &sf_info);
    if (!sndfile || sf_info.samplerate < 11000 || sf_info.channels < 1 || sf_info.frames < MIN_FRAMES) {
        fprintf(stderr,"loadClip(): Wrong sample file.\n");
        sf_close(sndfile);
        return 0;
    }
    int channels = sf_info.channels;
    sf_count_t frames = sf_info.frames;
    if (end == 0)
        end = frames;
    int dur = end - start;
    if (dur < frames && dur > 0)
        frames = dur;
    uint8_t src = samplerate != sf_info.samplerate;
    size_t size = frames * sf_info.channels * sizeof(float);
    if (size == 0) {
        fprintf(stderr,"loadClip(): Sample file has no data.\n");
        sf_close(sndfile);
        return 0;
    }

    float* data_in = (float*)malloc(size);
    if (!data_in) {
        fprintf(stderr,"loadClip(): Can't reserve memory (%d bytes) to load sample data.\n", size);
        sf_close(sndfile);
        return 0;
    }
    sf_seek(sndfile, start, SEEK_SET);
    sf_count_t count = sf_readf_float(sndfile, data_in, frames);
    sf_close(sndfile);

    if (count != frames) {
        fprintf(stderr,"loadClip(): Error reading %d frames of sample data.\n", frames);
        free(data_in);
        return 0;
    }

    // Deinterleave source audio into array of buffers data_deinterleaved[]
    float* data_deinterleaved[channels];
    for (int ch = 0; ch < channels; ch++) {
        data_deinterleaved[ch] = malloc(frames * sizeof(float));
        for (sf_count_t i = 0; i < frames; i++) {
            data_deinterleaved[ch][i] = data_in[i * channels + ch];
        }
    }
    free(data_in);

    // --------------------------------------------------------------
    // Re-sample
    // --------------------------------------------------------------

    if (samplerate != sf_info.samplerate) {
        // SRC each channel into array of buffers data_resampled[]
        double resample_ratio = (double)samplerate / sf_info.samplerate;
        sf_count_t max_resampled_frames = frames * resample_ratio + 1;

        float* data_resampled[channels];
        sf_count_t resampled_frames = 0;

        int ch;
        for (ch = 0; ch < channels; ++ch) {
            data_resampled[ch] = malloc(max_resampled_frames * sizeof(float));

            SRC_DATA src_data = {
                .data_in = data_deinterleaved[ch],
                .data_out = data_resampled[ch],
                .input_frames = frames,
                .output_frames = max_resampled_frames,
                .src_ratio = resample_ratio,
                .end_of_input = SF_TRUE
            };

            if (quality > 4)
                quality = 4;

            int err;
            SRC_STATE *src = src_new(quality, 1, &err);
            if (!src) {
                free(data_resampled[ch]);
                error = ERROR_SRC;
                fprintf(stderr, "loadClip(): SRC init error: %s\n", src_strerror(err));
                break;
            }

            if ((err = src_process(src, &src_data))) {
                src_delete(src);
                free(data_resampled[ch]);
                error = ERROR_SRC;
                fprintf(stderr, "loadClip(): SRC process error: %s\n", src_strerror(err));
                break;
            }

            src_delete(src);
            free(data_deinterleaved[ch]);
            data_deinterleaved[ch] = data_resampled[ch];
            if (ch == channels - 1)
                frames = src_data.output_frames_gen;
        }

        if (error) {
            for (int i = ch; i < channels; ++i)
                free(data_deinterleaved[i]);
            return 0;
        }
    }

    // --------------------------------------------------------------
    // Time stretch
    // --------------------------------------------------------------

    // Calculate timestretch ratio from playing tempo, number of beats and duration
    uint8_t timestretch;
    float ratio;
    if (tempo == 0) {
        timestretch = 0;
        ratio = 1.0;
    } else {
        ratio = (60 * samplerate * nbeats) / (tempo * frames);
        if (ratio < 0.01 || ratio > 100) {
            // Don't stretch if excessive stretch requested.
            ratio = 1.0;
            timestretch = 0;
        } else {
            timestretch = (fabs(ratio - 1.0) > 0.0001);
        }
    }

    printf("loadClip('%s', %d BEATS at %f BPM) => RATIO=%f (%d)\n", path, nbeats, tempo, ratio, timestretch);

    if (timestretch) {
        // Rubberband Options
        uint32_t rb_options =
            //RubberBandOptionEngineFaster |
            RubberBandOptionEngineFiner |
            RubberBandOptionProcessOffline |
            //RubberBandOptionProcessRealTime |
            // Transients Options => Only affect Faster Engine
            //RubberBandOptionTransientsCrisp |   // Default
            //RubberBandOptionTransientsMixed |
            //RubberBandOptionTransientsSmooth |
            //RubberBandOptionDetectorCompound |  // Default
            //RubberBandOptionDetectorSoft |
            //RubberBandOptionDetectorPercussive |
            //RubberBandOptionPhaseLaminar |      // Default
            //RubberBandOptionPhaseIndependent |
            //RubberBandOptionPitchHighSpeed |    // Default
            RubberBandOptionPitchHighQuality |
            //RubberBandOptionFormantShifted |    // Default
            RubberBandOptionFormantPreserved |
            RubberBandOptionThreadingAuto;

        // Create and setup rubberband stretcher object
        RubberBandState rb = rubberband_new(
            samplerate,
            channels,
            rb_options,
            ratio,
            1.0  // No pitch change
        );
        rubberband_set_expected_input_duration(rb, frames);
        // Study stage (off-line processing)
        //printf("Rubberband Study ...\n");
        rubberband_study(rb, (const float* const*)data_deinterleaved, frames, 1);

        // Calculate result size and setup an array for the result
        const int block_size = MIN_FRAMES;
        sf_count_t max_stretched_frames = (int)(frames * ratio) + block_size;
        float* data_stretched[channels];
        for (int ch = 0; ch < channels; ch++)
            data_stretched[ch] = malloc(max_stretched_frames * sizeof(float));

        // Timestretch the sample data (off-line processing)
        //rubberband_set_max_process_size(rb, block_size);
        sf_count_t frames_stretched = 0;
        for (sf_count_t i = 0; i < frames; i += block_size) {
            int n, final = 0;
            if (i + block_size >= frames) {
                n = frames - i;
                final = 1;
            } else {
                n = block_size;
            }
            float* block_in[channels];
            for (int ch = 0; ch < channels; ch++)
                block_in[ch] = data_deinterleaved[ch] + i;

            rubberband_process(rb, (const float* const*)block_in, n, final);
            int available = rubberband_available(rb);
            if (available > 0) {
                float *block_out[channels];
                for (int ch = 0; ch < channels; ch++)
                    block_out[ch] = data_stretched[ch] + frames_stretched;
                rubberband_retrieve(rb, block_out, available);
                frames_stretched += available;
            }
        }
        //printf("Stretched frames => %d (calc %d)\n", frames_stretched, (int)(frames * ratio));
        // Move the result array to the right place
        for (int ch = 0; ch < channels; ch++) {
            free(data_deinterleaved[ch]);
            data_deinterleaved[ch] = data_stretched[ch];
        }
        frames = frames_stretched;    // = (int)(frames * ratio) =>  Offline processing. With RT processing this is not true.

        rubberband_delete(rb);
    }

    //---------------------------------------------------------------
    // Create & setup new clip with the processed sample data
    //---------------------------------------------------------------

    uint8_t id = note - 1;

    // Create a new clip instance
    Clip* clip = NULL;
    clip = malloc(sizeof(Clip));
    if (!clip) {
        fprintf(stderr, "loadClip(): Clippy error: failed to create new clip object\n");
        sf_close(sndfile);
        return 0;
    }
    // Setup Clip parameters
    clip->gain = 1.0f;
    strcpy(clip->path, path);
    if (channels <= 2)
        clip->channels = channels;
    else
        clip->channels = 2;
    for (int i = 0; i < channels; i++) {
        if (i < clip->channels) {
            // Assign channel data to the clip
            clip->data[i] = data_deinterleaved[i];
            clip->alloc[i] = data_deinterleaved[i];
        } else {
            // Free uneeded channel data => TODO Limit this above in the process!!
            free(data_deinterleaved[i]);
            data_deinterleaved[i] = NULL;
        }
    }
    if (clip->channels == 1) {
        clip->data[1] = clip->data[0];
        clip->alloc[1] = NULL;
    }
    clip->frames = frames;
    clip->nbeats = nbeats;
    clip->start = start;
    clip->end = end;
    clip->quality = quality;
    clip->tempo = tempo;
    clip->tempo_lock = tempo_lock;
    clip->state = STATE_READY;

    // Re-sync if playing
    uint8_t curclip = (id == player->current_clip_id);
    unloadClip(channel, note);
    player->clips[id] = clip;
    if (curclip) {
        getMutex();
        player->current_clip = clip;
        player->current_clip_id = id;
        if (player->state == STATE_IDLE)
            player->state = STATE_SYNCYNG;
        releaseMutex();
    }
    //fprintf(stderr, "loadClip(channel=%u, note=%u, path=%s) id=%u\n", channel, note, path, id);
    return note;
}

uint8_t unloadClip(uint8_t channel, uint8_t note) {
    if(channel >= 16)
        return ERROR_RANGE;
    Player* player = players[channel];
    if(player == NULL)
        return ERROR_RANGE;
    uint8_t id = note - 1;
    if(id >= MAX_CLIPS)
        return ERROR_RANGE;
    // Abort any recording targeting this clip slot
    if ((g_recorder.state == REC_ARMED || g_recorder.state == REC_RECORDING) && g_recorder.channel == channel && g_recorder.clip_id == id) {
        getMutex();
        g_recorder.state = REC_ABORTED;
        releaseMutex();
    }
    // Stop any latency tail capture before its clip buffer is freed below
    if (g_recorder.state == REC_FINISHING && g_recorder.channel == channel && g_recorder.clip_id == id) {
        getMutex();
        g_recorder.tail[0] = NULL;
        g_recorder.tail[1] = NULL;
        g_recorder.state = REC_DONE;
        releaseMutex();
    }
    Clip* clip = player->clips[id];
    if(clip == NULL)
        return ERROR_RANGE;
    if (player->current_clip_id == id) {
        getMutex();
        player->current_clip = NULL;
        player->current_clip_id = -1;
        releaseMutex();
    }
    if (clip == g_recorder.committed_clip)
        g_recorder.committed_clip = NULL;
    player->clips[id] = NULL;
    for (int i=0; i < clip->channels; i++)
        free(clip->alloc[i]);
    free(clip);
    return ERROR_SUCCESS;
}

static void prefaultBuffer(float* buffer, size_t frames) {
    // calloc maps untouched zero pages; without this, first-touch page
    // faults land in the process() capture memcpy on the JACK thread,
    // one per 4KB for the whole take. Volatile stores so the compiler
    // can't elide zero writes to memory it knows calloc zeroed.
    volatile char* p = (volatile char*)buffer;
    size_t bytes = frames * sizeof(float);
    for (size_t offset = 0; offset < bytes; offset += 4096)
        p[offset] = 0;
    if (bytes)
        p[bytes - 1] = 0;
}

uint8_t armRecord(uint8_t channel, uint8_t note, uint8_t channels, float tempo, uint32_t max_frames) {
    // Auto-clean a finished / aborted recording
    if (g_recorder.state == REC_DONE || g_recorder.state == REC_ABORTED || g_recorder.state == REC_OVERFLOW)
        disarmRecord();
    if (g_recorder.state != REC_IDLE)
        return ERROR_EXISTS;
    if (channel >= 16 || note == 0 || note > MAX_CLIPS || channels < 1 || channels > 2)
        return ERROR_RANGE;
    if (!players[channel])
        return ERROR_RANGE;
    if (max_frames == 0)
        max_frames = MAX_DURATION * samplerate;
    Clip* clip = malloc(sizeof(Clip));
    if (!clip)
        return ERROR_CREATE;
    memset(clip, 0, sizeof(Clip));
    clip->gain = 1.0f;
    clip->state = STATE_IDLE;
    // calloc => any uncaptured latency tail is silence
    g_recorder.data[0] = calloc(max_frames, sizeof(float));
    g_recorder.data[1] = (channels == 2) ? calloc(max_frames, sizeof(float)) : NULL;
    if (!g_recorder.data[0] || (channels == 2 && !g_recorder.data[1])) {
        free(g_recorder.data[0]);
        free(g_recorder.data[1]);
        g_recorder.data[0] = NULL;
        g_recorder.data[1] = NULL;
        free(clip);
        return ERROR_CREATE;
    }
    // Still on the UI thread: fault the capture pages in before arming
    prefaultBuffer(g_recorder.data[0], max_frames);
    if (g_recorder.data[1])
        prefaultBuffer(g_recorder.data[1], max_frames);
    g_recorder.channel = channel;
    g_recorder.clip_id = note - 1;
    g_recorder.channels = channels;
    g_recorder.max_frames = max_frames;
    g_recorder.frames = 0;
    g_recorder.beats = 0;
    g_recorder.tempo = tempo;
    g_recorder.latency = 0;
    g_recorder.loop_frames = 0;
    g_recorder.target_frames = 0;
    g_recorder.tail[0] = NULL;
    g_recorder.tail[1] = NULL;
    g_recorder.pending_clip = clip;
    g_recorder.old_clip = NULL;
    g_recorder.committed_clip = NULL;
    getMutex();
    g_recorder.state = REC_ARMED;
    releaseMutex();
    return ERROR_SUCCESS;
}

uint8_t disarmRecord() {
    // Shrink the committed clip's buffers from the armRecord() capacity
    // (default 120s/channel) down to what the take actually used, else every
    // recorded clip retains the full capture allocation for its lifetime.
    // The copy happens here on the UI thread while the audio thread can
    // still play the old buffers; it only ever sees the swap, under mutex.
    // Skipped while REC_FINISHING: the tail capture still writes data[].
    // Atomically claim the clip: disarmRecord can be entered concurrently
    // (save thread vs reset()/end()), and two claimants would both free
    // the displaced buffers. Acquire pairs with the audio thread's
    // FINISHING->DONE store so the tail/fade writes are visible here.
    Clip* clip = __atomic_exchange_n(&g_recorder.committed_clip, NULL, __ATOMIC_ACQ_REL);
    if (clip && __atomic_load_n(&g_recorder.state, __ATOMIC_ACQUIRE) == REC_DONE) {
        uint32_t offset = (uint32_t)(clip->data[0] - clip->alloc[0]);
        uint32_t used = offset + clip->frames;
        if (used < g_recorder.max_frames) {
            float* shrunk[2] = {NULL, NULL};
            uint8_t ok = 1;
            for (int i = 0; i < clip->channels; i++) {
                shrunk[i] = malloc(used * sizeof(float));
                if (shrunk[i])
                    memcpy(shrunk[i], clip->alloc[i], used * sizeof(float));
                else
                    ok = 0;
            }
            if (ok) {
                float* old[2] = {clip->alloc[0], clip->alloc[1]};
                getMutex();
                for (int i = 0; i < clip->channels; i++) {
                    clip->alloc[i] = shrunk[i];
                    clip->data[i] = shrunk[i] + offset;
                }
                if (clip->channels == 1)
                    clip->data[1] = clip->data[0];
                releaseMutex();
                for (int i = 0; i < clip->channels; i++)
                    free(old[i]);
            } else {
                free(shrunk[0]);
                free(shrunk[1]);
            }
        }
    }
    getMutex();
    g_recorder.state = REC_IDLE;
    // tail[] alias the committed clip's buffer (owned by the clip) => drop, don't free
    g_recorder.tail[0] = NULL;
    g_recorder.tail[1] = NULL;
    releaseMutex();
    // Free anything not handed over to a committed clip
    free(g_recorder.data[0]);
    free(g_recorder.data[1]);
    g_recorder.data[0] = NULL;
    g_recorder.data[1] = NULL;
    if (g_recorder.pending_clip) {
        free(g_recorder.pending_clip);
        g_recorder.pending_clip = NULL;
    }
    if (g_recorder.old_clip) {
        for (int i = 0; i < g_recorder.old_clip->channels; i++)
            free(g_recorder.old_clip->alloc[i]);
        free(g_recorder.old_clip);
        g_recorder.old_clip = NULL;
    }
    return ERROR_SUCCESS;
}

uint8_t getRecordState() {
    return g_recorder.state;
}

uint8_t getRecordChannel() {
    return g_recorder.channel;
}

uint8_t getRecordNote() {
    return g_recorder.clip_id + 1;
}

uint32_t getRecordedFrames() {
    // After commit, frames keeps counting the latency tail => report the loop length
    if (g_recorder.state == REC_FINISHING || g_recorder.state == REC_DONE)
        return g_recorder.loop_frames;
    return g_recorder.frames;
}

uint16_t getRecordedBeats() {
    return g_recorder.beats;
}

void setRecordLatencyOffset(int32_t frames) {
    g_record_latency_offset = frames;
}

int32_t getRecordLatencyOffset() {
    return g_record_latency_offset;
}

void setRecordMonitorAuto(uint8_t enable) {
    g_record_monitor_auto = enable ? 1 : 0;
}

void setInputMonitor(uint8_t channel, uint8_t enable) {
    if (channel >= 16 || !players[channel])
        return;
    players[channel]->monitor = enable ? 1 : 0;
}

uint8_t getInputMonitor(uint8_t channel) {
    if (channel >= 16 || !players[channel])
        return 0;
    return players[channel]->monitor;
}

void setMonitorRoute(uint8_t direct) {
    monitor_direct = direct ? 1 : 0;
}

uint8_t getMonitorRoute() {
    return monitor_direct;
}

void setMonitorGain(float gain) {
    if (gain < 0.0f)
        gain = 0.0f;
    monitor_gain = gain;
}

float getMonitorGain() {
    return monitor_gain;
}

int saveClip(uint8_t channel, uint8_t note, const char* path) {
    if (channel >= 16 || note == 0 || note > MAX_CLIPS || !path)
        return 1;
    Player* player = players[channel];
    if (!player)
        return 1;
    Clip* clip = player->clips[note - 1];
    if (!clip)
        return 1;
    if (strlen(path) >= sizeof(clip->path)) {
        fprintf(stderr, "saveClip(): Path too long '%s'.\n", path);
        return 1;
    }
    if (saveFile(path, clip->data, samplerate, clip->channels, clip->frames))
        return 1;
    strcpy(clip->path, path);
    return 0;
}

float toDb(float val) {
    return 20.0 * log10(val);
}

float fromDb(float val) {
    return pow(10.0, val / 20.0);
}

uint8_t setGain(uint8_t channel, uint8_t id, float gain) {
    if (channel > 15)
        return ERROR_RANGE;
    Player* player = players[channel];
    if (!player)
        return ERROR_RANGE;
    if (id >= MAX_CLIPS)
        return ERROR_RANGE;
    Clip* clip = player->clips[id];
    if (!clip)
        return ERROR_RANGE;
    clip->gain = fromDb(gain);
    return ERROR_SUCCESS;
}

float getGain(uint8_t channel, uint8_t id) {
    if (channel > 15)
        return 0.0f;
    Player* player = players[channel];
    if (!player)
        return 0.0f;
    if (id >= MAX_CLIPS)
        return 0.0f;
    Clip* clip = player->clips[id];
    if (!clip)
        return 0.0f;
    return toDb(clip->gain);
}

uint32_t getFileSamplerate(const char* path) {
    SF_INFO sf_info;
    memset(&sf_info, 0, sizeof(sf_info));
    SNDFILE* sndfile = sf_open(path, SFM_READ, &sf_info);
    if (!sndfile)
        return 0;
    sf_close(sndfile);
    return sf_info.samplerate;
}

uint32_t getFileFrames(const char* path) {
    SF_INFO sf_info;
    memset(&sf_info, 0, sizeof(sf_info));
    SNDFILE* sndfile = sf_open(path, SFM_READ, &sf_info);
    if (!sndfile)
        return 0;
    sf_close(sndfile);
    return sf_info.frames;
}

int saveFile(const char* dst_path, float *data[], int samplerate, int channels, uint32_t frames) {
    // Interleave into buffer data_out
    float *data_out = malloc(frames * channels * sizeof(float));
    if (!data_out) {
        fprintf(stderr, "saveFile(): Failed to reserve memory (%d bytes).\n", frames * channels * sizeof(float));
        return 1;
    }
    for (int ch = 0; ch < channels; ch++) {
        for (sf_count_t i = 0; i < frames; i++) {
            data_out[i * channels + ch] = data[ch][i];
        }
    }
    // Write output
    SF_INFO sf_info;
    memset(&sf_info, 0, sizeof(sf_info));
    sf_info.samplerate = samplerate;
    sf_info.channels = channels;
    //sf_info.format = SF_FORMAT_WAV | SF_FORMAT_PCM_16;
    sf_info.format = SF_FORMAT_WAV | SF_FORMAT_FLOAT;
    SNDFILE *outfile = sf_open(dst_path, SFM_WRITE, &sf_info);
    if (!outfile) {
        free(data_out);
        fprintf(stderr, "saveFile(): Failed to open output file '%s'.\n", dst_path);
        return 1;
    }
    sf_writef_float(outfile, data_out, frames);
    sf_close(outfile);
    free(data_out);

    return 0;
}
