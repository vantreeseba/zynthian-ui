/*  Declares LinkSync class providing Ableton Link session synchronisation
 *
 *   Copyright (c) 2026 Zynthian Project
 *
 *   This program is free software; you can redistribute it and/or modify
 *   it under the terms of the GNU General Public License as published by
 *   the Free Software Foundation; either version 2 of the License, or
 *   (at your option) any later version.
 *
 *   This program is distributed in the hope that it will be useful,
 *   but WITHOUT ANY WARRANTY; without even the implied warranty of
 *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *   GNU General Public License for more details.
 *
 *   You should have received a copy of the GNU General Public License
 *   along with this program; if not, write to the Free Software
 *   Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.
*/

#pragma once

#include <cstddef>
#include <cstdint>

// Range of tempo the sequencer can run at. A session outside this range is followed
// at a power-of-two multiple of its tempo so that we stay musically in time with it.
#define LINK_TEMPO_MIN 10.0
#define LINK_TEMPO_MAX 500.0

/** Snapshot of the Link session, sampled once per JACK period.

    All values describe the state at the moment the audio for the period that was
    passed to LinkSync::audioUpdate() reaches the output, i.e. the start of the
    period plus the audio output latency.
*/
struct LinkState {
    double tempo;          // Tempo in beats per minute, scaled into the supported range
    double beat;           // Beat magnitude on the session timeline (local to this peer)
    double phase;          // Phase within the launch quantum, in beats, range [0, quantum)
    double barPhase;       // Phase within the bar, in beats, range [0, beatsPerBar)
    bool isPlaying;        // Session start/stop state
    bool isPlayingChanged; // True if the session (not this peer) just changed isPlaying
};

/** Audio to publish to the Link session for the current period.

    Samples are the usual JACK float format and are converted to the 16 bit integer
    format Link Audio transmits. Left alone, nothing is published.
*/
struct LinkAudioOut {
    const float* pLeft;       // Left channel samples, or NULL to publish nothing
    const float* pRight;      // Right channel samples, or NULL to publish mono
    std::uint32_t sampleRate; // Sample rate of the samples, in Hz
};

/** LinkSync class wraps an Ableton Link instance.

    Ownership of the Link session is split between two threads:

    - The application thread (Python/ctypes) only ever *requests* changes. Requests
      are stored in atomics and applied later by the audio thread. This keeps every
      public setter safe to call from any thread without blocking the audio thread.
    - The audio (JACK process) thread samples the session and applies pending
      requests in audioUpdate(), using Link's realtime-safe audio session state API.

    Requests are applied whether or not Link is enabled, so the session timeline
    always mirrors the local tempo and transport state. Link stamps changes made
    whilst disabled as old, so joining a session adopts the session's tempo rather
    than imposing ours upon it.

    The only exceptions are enable()/enableStartStopSync()/numPeers(), which are
    thread-safe but NOT realtime-safe, so they must not be called from the audio
    thread.
*/
class LinkSync {
  public:
    /** @brief  Construct Link session (disabled until enable(true) is called)
        @param  tempo Initial session tempo in beats per minute
        @param  name Name identifying this peer to the session, e.g. the hostname
    */
    LinkSync(double tempo, const char* name);

    /** @brief  Destruction called when object destroyed
    */
    ~LinkSync();

    LinkSync(const LinkSync&) = delete;
    LinkSync& operator=(const LinkSync&) = delete;

    /** @brief  Enable / disable Link (joins or leaves the network session)
        @param  enable True to enable
        @note   Not realtime-safe - do not call from the audio thread
    */
    void enable(bool enable);

    /** @brief  Check whether Link is enabled
        @retval bool True if enabled
    */
    bool isEnabled() const;

    /** @brief  Enable / disable sharing of transport start/stop with the session
        @param  enable True to enable
        @note   Not realtime-safe - do not call from the audio thread
    */
    void enableStartStopSync(bool enable);

    /** @brief  Check whether start/stop sync is enabled
        @retval bool True if enabled
    */
    bool isStartStopSyncEnabled() const;

    /** @brief  Get quantity of peers connected to the session
        @retval size_t Quantity of peers (not including this peer)
        @note   Not realtime-safe - do not call from the audio thread
    */
    std::size_t numPeers() const;

    /** @brief  Enable / disable publishing this device's audio to the Link session
        @param  enable True to announce an audio channel to the session
        @note   Not realtime-safe - do not call from the audio thread
        @note   Has no effect until Link itself is enabled
    */
    void enableAudio(bool enable);

    /** @brief  Check whether publishing audio to the Link session is enabled
        @retval bool True if enabled
    */
    bool isAudioEnabled() const;

    /** @brief  Set the audio output latency, which is compensated for when placing
                the session timeline against the audio stream
        @param  micros Latency between the audio thread writing a sample and that
                sample leaving the hardware, in microseconds
        @note   Thread-safe. Call from the JACK latency callback
    */
    void setOutputLatency(std::int64_t micros);

    /** @brief  Request a session tempo change
        @param  tempo Tempo in beats per minute
        @note   Thread-safe. The change is applied by the audio thread on its next period
    */
    void requestTempo(double tempo);

    /** @brief  Request a session transport start/stop
        @param  playing True to start transport
        @note   Thread-safe. Ignored by peers unless start/stop sync is enabled
    */
    void requestIsPlaying(bool playing);

    /** @brief  Sample the session and apply pending requests
        @param  sampleTime Audio server's frame clock at the start of this JACK period
        @param  frames Quantity of frames in this JACK period
        @param  quantum Quantity of beats in the launch grid (a whole quantity of bars)
        @param  beatsPerBar Quantity of beats in each bar
        @param  pState Pointer to structure populated with the sampled session state
        @param  pOut Pointer to the audio to publish to the session, or NULL to publish
                nothing this period
        @retval bool True if state was sampled (false if Link is disabled)
        @note   Realtime-safe. Must only be called from the audio thread
    */
    bool audioUpdate(std::uint64_t sampleTime, std::uint32_t frames, double quantum, double beatsPerBar,
                     LinkState* pState, const LinkAudioOut* pOut);

    /** @brief  Ask the session to put a downbeat of the launch quantum within the period
                last passed to audioUpdate(), because our bar grid was just moved there
        @param  frameOffset Frames into the period at which the downbeat falls
        @param  sampleRate Sample rate in Hz
        @param  quantum Quantity of beats in the launch grid
        @note   Realtime-safe. Must only be called from the audio thread
        @note   Alone in a session the grid moves to us. Otherwise the session keeps its
                grid and we are steered back onto it.
    */
    void alignDownbeat(std::uint32_t frameOffset, std::uint32_t sampleRate, double quantum);

    /** @brief  Request a reset of the sample-time to host-time mapping
        @note   Thread-safe. Applied by the audio thread on its next period. Call when the
                relationship between the frame clock and time changes, e.g. sample rate change
    */
    void resetTimeFilter();

  private:
    struct Impl;
    Impl* m_pImpl;
};
