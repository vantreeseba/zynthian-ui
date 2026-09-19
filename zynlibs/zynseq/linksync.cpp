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

#include <ableton/Link.hpp>
#include <ableton/link/HostTimeFilter.hpp>

#include <atomic>

struct LinkSync::Impl {
    Impl(double tempo)
        : link(tempo) {}

    ableton::Link link;
    ableton::link::HostTimeFilter<ableton::link::platform::Clock> timeFilter;
    double sampleTime = 0.0;

    // Mirrors of the Link flags so the audio thread never has to ask Link
    std::atomic<bool> enabled{false};
    std::atomic<bool> startStopSync{false};
    // Pending requests from the application thread. 0.0 / -1 mean "nothing pending"
    std::atomic<double> pendingTempo{0.0};
    std::atomic<int> pendingPlay{-1};
};

LinkSync::LinkSync(double tempo)
    : m_pImpl(new Impl(tempo)) {
}

LinkSync::~LinkSync() {
    m_pImpl->link.enable(false);
    delete m_pImpl;
}

void LinkSync::enable(bool enable) {
    if (enable == m_pImpl->enabled.load(std::memory_order_relaxed))
        return;
    if (enable) {
        // Start from a clean sample-time to host-time mapping: the audio stream may
        // have been running (or not) for an arbitrary time before we joined
        m_pImpl->timeFilter.reset();
        // Drop any request that was left pending while disabled
        m_pImpl->pendingTempo.store(0.0, std::memory_order_relaxed);
        m_pImpl->pendingPlay.store(-1, std::memory_order_relaxed);
    }
    // Set our mirror before leaving, after joining, so the audio thread never
    // touches a session that is not up
    if (!enable)
        m_pImpl->enabled.store(false, std::memory_order_release);
    m_pImpl->link.enable(enable);
    if (enable)
        m_pImpl->enabled.store(true, std::memory_order_release);
}

bool LinkSync::isEnabled() const {
    return m_pImpl->enabled.load(std::memory_order_acquire);
}

void LinkSync::enableStartStopSync(bool enable) {
    m_pImpl->link.enableStartStopSync(enable);
    m_pImpl->startStopSync.store(enable, std::memory_order_release);
}

bool LinkSync::isStartStopSyncEnabled() const {
    return m_pImpl->startStopSync.load(std::memory_order_acquire);
}

std::size_t LinkSync::numPeers() const {
    return m_pImpl->link.numPeers();
}

void LinkSync::requestTempo(double tempo) {
    if (tempo >= 10.0 && tempo < 500.0)
        m_pImpl->pendingTempo.store(tempo, std::memory_order_relaxed);
}

void LinkSync::requestIsPlaying(bool playing) {
    m_pImpl->pendingPlay.store(playing ? 1 : 0, std::memory_order_relaxed);
}

void LinkSync::resetTimeFilter() {
    m_pImpl->timeFilter.reset();
}

bool LinkSync::audioUpdate(std::uint32_t frames, std::uint32_t sampleRate, double quantum, LinkState* pState) {
    // Keep the sample-time to host-time mapping warm even while disabled so that
    // enabling Link does not have to wait for the filter to converge
    const auto hostTime = m_pImpl->timeFilter.sampleTimeToHostTime(m_pImpl->sampleTime);
    m_pImpl->sampleTime += frames;

    if (!m_pImpl->enabled.load(std::memory_order_acquire))
        return false;

    (void)sampleRate;

    auto state = m_pImpl->link.captureAudioSessionState();
    bool bCommit = false;

    const double tempo = m_pImpl->pendingTempo.exchange(0.0, std::memory_order_relaxed);
    if (tempo > 0.0) {
        state.setTempo(tempo, hostTime);
        bCommit = true;
    }

    const int play = m_pImpl->pendingPlay.exchange(-1, std::memory_order_relaxed);
    if (play >= 0) {
        // Request beat 0 at the start time so that peers launch in phase with us
        state.setIsPlayingAndRequestBeatAtTime(play != 0, hostTime, 0.0, quantum);
        bCommit = true;
    }

    if (bCommit)
        m_pImpl->link.commitAudioSessionState(state);

    pState->tempo = state.tempo();
    pState->beat = state.beatAtTime(hostTime, quantum);
    pState->phase = state.phaseAtTime(hostTime, quantum);
    pState->isPlaying = state.isPlaying();
    return true;
}
