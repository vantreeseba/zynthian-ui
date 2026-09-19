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

/** Snapshot of the Link session, sampled once per JACK period.

    All values describe the state at the start of the period that was passed to
    LinkSync::audioUpdate().
*/
struct LinkState {
    double tempo;     // Session tempo in beats per minute
    double beat;      // Beat magnitude on the session timeline (local to this peer)
    double phase;     // Phase within the quantum, in beats, range [0, quantum)
    bool isPlaying;   // Session start/stop state (only meaningful with start/stop sync enabled)
};

/** LinkSync class wraps an Ableton Link instance.

    Ownership of the Link session is split between two threads:

    - The application thread (Python/ctypes) only ever *requests* changes. Requests
      are stored in atomics and applied later by the audio thread. This keeps every
      public setter safe to call from any thread without blocking the audio thread.
    - The audio (JACK process) thread samples the session and applies pending
      requests in audioUpdate(), using Link's realtime-safe audio session state API.

    The only exceptions are enable()/enableStartStopSync()/numPeers(), which are
    thread-safe but NOT realtime-safe, so they must not be called from the audio
    thread.
*/
class LinkSync {
  public:
    /** @brief  Construct Link session (disabled until enable(true) is called)
        @param  tempo Initial session tempo in beats per minute
    */
    LinkSync(double tempo);

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
        @param  frames Quantity of frames in this JACK period
        @param  sampleRate Sample rate in frames per second
        @param  quantum Quantity of beats in the sync grid (beats per bar)
        @param  pState Pointer to structure populated with the sampled session state
        @retval bool True if state was sampled (false if Link is disabled)
        @note   Realtime-safe. Must only be called from the audio thread
    */
    bool audioUpdate(std::uint32_t frames, std::uint32_t sampleRate, double quantum, LinkState* pState);

    /** @brief  Reset the sample-time to host-time mapping
        @note   Call when the audio stream is interrupted, e.g. on sample rate change
    */
    void resetTimeFilter();

  private:
    struct Impl;
    Impl* m_pImpl;
};
