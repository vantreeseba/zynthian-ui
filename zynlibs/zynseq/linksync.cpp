/*  Implements LinkSync class providing Ableton Link session synchronisation
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

#include "linksync.h"

#include <ableton/LinkAudio.hpp>
#include <ableton/link/HostTimeFilter.hpp>

#include <atomic>
#include <chrono>
#include <cmath>
#include <memory>

namespace {

/*  Factor to apply to a session tempo to bring it into the range the sequencer
    supports. Always a power of two, so that the scaled tempo remains musically
    related to the session (half time, double time, ...).
*/
double tempoScale(double tempo) {
    double scale = 1.0;
    if (!(tempo > 0.0))
        return scale;
    while (tempo * scale >= LINK_TEMPO_MAX)
        scale *= 0.5;
    while (tempo * scale < LINK_TEMPO_MIN)
        scale *= 2.0;
    return scale;
}

/*  Convert a JACK sample to the 16 bit integer format Link Audio transmits, clipping
    rather than wrapping around on samples beyond full scale.
*/
std::int16_t floatToPcm(float sample) {
    if (sample > 1.0f)
        sample = 1.0f;
    else if (sample < -1.0f)
        sample = -1.0f;
    return (std::int16_t)lrintf(sample * 32767.0f);
}

// Furthest the filtered host time may stray from the clock before the filter is reset
const std::chrono::microseconds LINK_FILTER_MAX_DRIFT{100000};

} // namespace

struct LinkSync::Impl {
    Impl(double tempo, const char* name)
        : link(tempo, name ? name : "zynthian")
        , sink(link, name ? name : "zynthian", 2 * 1024) {}

    ableton::LinkAudio link;
    // Announced for the lifetime of the session. Link only transmits whilst a peer is
    // listening, so an idle sink costs nothing but its announcement.
    ableton::LinkAudioSink sink;
    // Only ever touched by the audio thread. Other threads ask for a reset.
    ableton::link::HostTimeFilter<ableton::link::platform::Clock> timeFilter;
    std::atomic<bool> filterReset{false};
    // Host time of the start of the period last passed to audioUpdate()
    std::chrono::microseconds periodHostTime{0};

    // Mirrors of the Link flags so the audio thread never has to ask Link
    std::atomic<bool> enabled{false};
    std::atomic<bool> startStopSync{false};
    std::atomic<bool> audioEnabled{false};
    // Pending requests from the application thread. 0.0 / -1 mean "nothing pending"
    std::atomic<double> pendingTempo{0.0};
    std::atomic<int> pendingPlay{-1};
    // Audio output latency, compensated for so that our audio leaves the hardware
    // in time with the session rather than in time with our audio callback
    std::atomic<long long> outputLatency{0};

    // Session start/stop state as of the previous period, for edge detection. Not
    // valid until the first period after enabling, so that joining a playing
    // session does not itself look like the session starting.
    std::atomic<bool> playValid{false};
    bool prevPlaying = false;
};

LinkSync::LinkSync(double tempo, const char* name)
    : m_pImpl(new Impl(tempo, name)) {
}

LinkSync::~LinkSync() {
    m_pImpl->link.enable(false);
    delete m_pImpl;
}

void LinkSync::enable(bool enable) {
    if (enable == m_pImpl->enabled.load(std::memory_order_relaxed))
        return;
    // Take a fresh start/stop baseline either way, so that neither joining a playing
    // session nor leaving one is mistaken for the session starting or stopping
    m_pImpl->playValid.store(false, std::memory_order_release);
    // Set our mirror before leaving, after joining, so the audio thread never
    // touches a session that is not up
    if (!enable)
        m_pImpl->enabled.store(false, std::memory_order_release);
    m_pImpl->link.enable(enable);
    if (enable) {
        // Link switches audio off with itself, so restore our setting on rejoining
        m_pImpl->link.enableLinkAudio(m_pImpl->audioEnabled.load(std::memory_order_acquire));
        m_pImpl->enabled.store(true, std::memory_order_release);
    }
}

bool LinkSync::isEnabled() const {
    return m_pImpl->enabled.load(std::memory_order_acquire);
}

void LinkSync::enableStartStopSync(bool enable) {
    // Do not act upon the state the session was already in when sync was turned on
    m_pImpl->playValid.store(false, std::memory_order_release);
    m_pImpl->link.enableStartStopSync(enable);
    m_pImpl->startStopSync.store(enable, std::memory_order_release);
}

bool LinkSync::isStartStopSyncEnabled() const {
    return m_pImpl->startStopSync.load(std::memory_order_acquire);
}

std::size_t LinkSync::numPeers() const {
    return m_pImpl->link.numPeers();
}

void LinkSync::enableAudio(bool enable) {
    m_pImpl->audioEnabled.store(enable, std::memory_order_release);
    m_pImpl->link.enableLinkAudio(enable);
}

bool LinkSync::isAudioEnabled() const {
    return m_pImpl->audioEnabled.load(std::memory_order_acquire);
}

void LinkSync::setOutputLatency(std::int64_t micros) {
    if (micros < 0)
        micros = 0;
    m_pImpl->outputLatency.store((long long)micros, std::memory_order_relaxed);
}

void LinkSync::requestTempo(double tempo) {
    if (tempo >= LINK_TEMPO_MIN && tempo < LINK_TEMPO_MAX)
        m_pImpl->pendingTempo.store(tempo, std::memory_order_relaxed);
}

void LinkSync::requestIsPlaying(bool playing) {
    m_pImpl->pendingPlay.store(playing ? 1 : 0, std::memory_order_relaxed);
}

void LinkSync::resetTimeFilter() {
    m_pImpl->filterReset.store(true, std::memory_order_release);
}

void LinkSync::alignDownbeat(std::uint32_t frameOffset, std::uint32_t sampleRate, double quantum) {
    if (!sampleRate)
        return;
    const auto time = m_pImpl->periodHostTime
                      + std::chrono::microseconds(llround(1.0e6 * frameOffset / sampleRate));
    auto state = m_pImpl->link.captureAudioSessionState();
    state.requestBeatAtTime(0.0, time, quantum / tempoScale(state.tempo()));
    m_pImpl->link.commitAudioSessionState(state);
}

bool LinkSync::audioUpdate(std::uint64_t sampleTime, std::uint32_t frames, double quantum, double beatsPerBar,
                           LinkState* pState, const LinkAudioOut* pOut) {
    // The mapping is kept warm even while disabled so that enabling Link does not
    // have to wait for the filter to converge. It is fed the audio server's frame
    // clock rather than a count of the frames we were given, so that periods lost to
    // an xrun do not shift the mapping.
    if (m_pImpl->filterReset.exchange(false, std::memory_order_acquire))
        m_pImpl->timeFilter.reset();
    auto filteredTime = m_pImpl->timeFilter.sampleTimeToHostTime((double)sampleTime);
    // The filter smooths out callback jitter, so it can only ever be a period or so
    // away from the clock. Any further means the frame clock jumped => start again.
    const auto drift = filteredTime - m_pImpl->link.clock().micros();
    if (drift > LINK_FILTER_MAX_DRIFT || drift < -LINK_FILTER_MAX_DRIFT) {
        m_pImpl->timeFilter.reset();
        filteredTime = m_pImpl->timeFilter.sampleTimeToHostTime((double)sampleTime);
    }
    // Latency compensation places the timeline at the moment this period's audio
    // leaves the hardware, which is what has to line up between peers - not the
    // moment we compute it.
    const auto hostTime = filteredTime
                          + std::chrono::microseconds(m_pImpl->outputLatency.load(std::memory_order_relaxed));
    m_pImpl->periodHostTime = hostTime;

    // Requests are applied whether or not we are enabled, so that the session
    // timeline always reflects the local tempo and transport state. Link stamps
    // changes made whilst disabled as old, so they never hijack a session we join.
    auto state = m_pImpl->link.captureAudioSessionState();
    bool bCommit = false;
    bool bSelfPlayChange = false;

    // Quantum as the session sees it: our quantum covers the same span of time even
    // when we are running at a multiple of the session tempo
    const double dQuantumIn = quantum / tempoScale(state.tempo());

    const double tempo = m_pImpl->pendingTempo.exchange(0.0, std::memory_order_relaxed);
    if (tempo > 0.0) {
        state.setTempo(tempo / tempoScale(state.tempo()), hostTime);
        bCommit = true;
    }

    const int play = m_pImpl->pendingPlay.exchange(-1, std::memory_order_relaxed);
    if (play > 0) {
        // Request beat 0 at the start time so that peers launch in phase with us. Never
        // whilst already playing: alone in a session the request is granted there and
        // then, which would jump the phase of a timeline everything is locked to.
        if (!state.isPlaying()) {
            state.setIsPlayingAndRequestBeatAtTime(true, hostTime, 0.0, dQuantumIn);
            bCommit = true;
            bSelfPlayChange = true;
        }
    } else if (play == 0) {
        // Stopping says nothing about the beat grid, so leave the timeline alone
        state.setIsPlaying(false, hostTime);
        bCommit = true;
        bSelfPlayChange = true;
    }

    if (bCommit)
        m_pImpl->link.commitAudioSessionState(state);

    const bool bEnabled = m_pImpl->enabled.load(std::memory_order_acquire);

    // Edge detect the session start/stop state, ignoring the changes we made above
    const bool bPlaying = state.isPlaying();
    bool bPlayingChanged = false;
    if (!bEnabled || !m_pImpl->playValid.load(std::memory_order_acquire)) {
        m_pImpl->playValid.store(true, std::memory_order_release);
    } else if (bPlaying != m_pImpl->prevPlaying) {
        bPlayingChanged = !bSelfPlayChange;
    }
    m_pImpl->prevPlaying = bPlaying;

    if (!bEnabled)
        return false;

    // Follow the session at a power-of-two multiple of its tempo if it is beyond
    // the range we can run at, so that we stay in time rather than falling out of it
    const double dScale = tempoScale(state.tempo());
    const double dQuantum = quantum / dScale;

    pState->tempo = state.tempo() * dScale;
    pState->beat = state.beatAtTime(hostTime, dQuantum) * dScale;
    pState->phase = state.phaseAtTime(hostTime, dQuantum) * dScale;
    pState->barPhase = (beatsPerBar > 0.0) ? fmod(pState->phase, beatsPerBar) : pState->phase;
    pState->isPlaying = bPlaying;
    pState->isPlayingChanged = bPlayingChanged;

    // Publish this period's audio to the session. Peers place it by beat rather than by
    // arrival, so the beat and quantum must be the ones the audio was rendered against,
    // and the host time already accounts for output latency.
    if (pOut && pOut->pLeft && m_pImpl->audioEnabled.load(std::memory_order_acquire)) {
        const std::size_t nChannels = pOut->pRight ? 2 : 1;
        const std::size_t nSamples = (std::size_t)frames * nChannels;
        // Takes effect on a later period, so this one is simply skipped if it is short
        m_pImpl->sink.requestMaxNumSamples(nSamples);
        ableton::LinkAudioSink::BufferHandle buffer(m_pImpl->sink);
        if (buffer && buffer.maxNumSamples >= nSamples) {
            std::int16_t* pDst = buffer.samples;
            if (nChannels == 2) {
                for (std::uint32_t i = 0; i < frames; ++i) {
                    *pDst++ = floatToPcm(pOut->pLeft[i]);
                    *pDst++ = floatToPcm(pOut->pRight[i]);
                }
            } else {
                for (std::uint32_t i = 0; i < frames; ++i)
                    *pDst++ = floatToPcm(pOut->pLeft[i]);
            }
            buffer.commit(state, state.beatAtTime(hostTime, dQuantum), dQuantum, frames, nChannels,
                          pOut->sampleRate);
        }
    }
    return true;
}
