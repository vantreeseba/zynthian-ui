#include <stdint.h> // Provides fixed width integer defininitions

#define MAX_CLIPS 127   // Maximum quantity of clips per player/channel
#define MIN_FRAMES 4096 // Minimum quantity of frames to allow in audio files
#define MAX_DURATION 120 // Maximum clip duration in seconds (also default record buffer capacity)

// Note-on velocities used by the zynseq=>clippy control protocol
#define CLIPPY_VEL_START 1     // Start clip (note 1..127) or stop player (note 0)
#define CLIPPY_VEL_RETRIG 3    // Retrigger (loop repeat) clip
#define CLIPPY_VEL_REC_START 5 // Punch-in: begin capture at event time
#define CLIPPY_VEL_REC_STOP 6  // Punch-out: commit recording as clip and start looping it
#define CLIPPY_VEL_REC_ABORT 7 // Abort recording without committing

enum STATE {
    STATE_IDLE,     // Not ready for use
    STATE_LOAD,     // Load new file into preload cache
    STATE_READY,    // Cached, ready for use
    STATE_STARTING, // Switch sndfile
    STATE_PLAYING,  // Buffer in use for playback (may be from preload or ring buffer)
    STATE_STOPPING,  // Fade to avoid stop clitch
    STATE_SYNCYNG
};

enum ERROR {
    ERROR_SUCCESS,      // No error
    ERROR_EXISTS,       // Already exists
    ERROR_RANGE,        // Parameter out of range
    ERROR_CREATE,       // Cannot create object
    ERROR_PORT,         // Cannot create port
    ERROR_OPEN,         // Error opening file
    ERROR_SAMPLERATE,   // Wrong samplerate
    ERROR_ACTIVATE,     // Cannot activate jack
    ERROR_SRC,          // Error during samplerate conversion
    ERROR_STRETCH       // Error during time stretch
};

enum REC_STATE {
    REC_IDLE,       // No recording armed
    REC_ARMED,      // Waiting for punch-in message
    REC_RECORDING,  // Capturing audio
    REC_DONE,       // Recording committed as clip (awaiting save/disarm)
    REC_ABORTED,    // Recording aborted (awaiting disarm)
    REC_OVERFLOW,   // Capture buffer exhausted, recording abandoned (awaiting disarm)
    REC_FINISHING   // Committed and looping; still capturing the latency tail (=> REC_DONE)
};

enum MIDI_COMMANDS {
    MIDI_NOTE_OFF   = 0x80,
    MIDI_NOTE_ON    = 0x90,
    MIDI_CC         = 0xb0,
    MIDI_POLYTOUCH  = 0xa0,
    MIDI_PROGRAM    = 0xc0,
    MIDI_AFTERTOUCH = 0xd0,
    MIDI_PITCHBEND  = 0xe0
};

// ***Function declarations***

/** @brief Change tempo (timestretch) of all loaded clips with tempo_lock flag set to False
    @param  tempo New tempo to recalculate timestretch
*/
void changeTempo(float tempo);

/** @brief Change clip tempo (timestretch)
    @param  channel MIDI channel
    @param  id Clip index
    @param  tempo New tempo to recalculate timestretch
*/
void changeClipTempo(uint8_t channel, uint8_t id, float tempo);

/** @brief Set IDLE_STATE in players that need to rewarp
*/
void idlePlayers();

/** @brief Set clip's tempo_lock flag
    @param  channel MIDI channel
    @param  id Clip index
    @param  tempo_lock When set, ignore global tempo changes => changeTempo()
*/
void changeClipTempoLock(uint8_t channel, uint8_t id, uint8_t tempo_lock);

/** @brief Set clip's beat count and native tempo without reloading or warping
    @param  channel MIDI channel of player (0-15)
    @param  id Id of clip (note - 1)
    @param  nbeats Number of beats in clip
    @param  tempo Tempo (BPM) at which the clip plays unstretched
    @note   Use after recording when the session tempo is derived from the take
*/
void setClipBeats(uint8_t channel, uint8_t id, uint16_t nbeats, float tempo);

/** @brief  Get the next available clip
    @param  channel MIDI channel
    @retval uint8_t Clip ID (MIDI note) or 0 on error
*/
uint8_t getFreeClip(uint8_t channel);

/** @brief  Load a file into a player
    @param  channel MIDI channel
    @param  note MIDI note to trigger clip or 0 for next available
    @param  path Full (or relative) path and filename
    @param  nbeats Total number of beats in sample file
    @param  start Start frame
    @param  end End frame
    @param  quality Re-sample quality
    @param  tempo Tempo in BPM to calculate timestratch (0 = no timestretch)
    @param  tempo_lock When set, ignore global tempo changes => changeTempo()
    @retval uint8_t Clip ID (MIDI note) or 0 on error
*/
uint8_t loadClip(uint8_t channel, uint8_t note, const char* path, uint16_t nbeats,
                 uint32_t start, uint32_t end, uint8_t quality, float tempo,
                 uint8_t tempo_lock);

/** @brief  Unload a file from a player
    @param  channel MIDI channel
    @param  note MIDI note to trigger clip
    @retval uint8_t Error code
*/
uint8_t unloadClip(uint8_t channel, uint8_t note);

/** @brief  Create a new clip player
    @brief  channel MIDI channel for new player or 255 for next available channel
    @retval uint8_t Channel number or 255 on error
*/
uint8_t addPlayer(uint8_t channel);

/** @brief  Remove a clip player
    @param  channel MIDI channel
    @retval uint8_t Error code
*/
uint8_t removePlayer(uint8_t channel);

/** @brief  Idle Player
    @param  channel MIDI channel
    @param  clip Index of clip to insert
    @retval uint8_t Error code
*/
uint8_t idlePlayerClip(uint8_t channel, uint8_t clip);

//!@todo Remove clip manipulation (insert, remove, swap).

/** @brief  Insert clip
    @param  channel MIDI channel
    @param  clip Index of clip to insert
    @retval uint8_t Error code
    @note   Moves existing clips up. Fails if no room.
*/
uint8_t insertClip(uint8_t channel, uint8_t clip);

/** @brief  Remove clip
    @param  channel MIDI channel
    @param  clip Index of clip to remove
    @retval uint8_t Error code
    @note   Moves existing clips down.
*/
uint8_t removeClip(uint8_t channel, uint8_t clip);

/** @brief  Nudge (move) a clip one position
    @param  channel MIDI channel
    @param  clip Index of clip
    @param  forward True to move the clip forward
    @retval uint8_t Error code
*/
uint8_t nudgeClip(uint8_t channel, uint8_t clip, uint8_t forward);

/** @brief  Return file path of loaded clip
    @param  channel MIDI channel
    @param  clip Index of clip
    @retval const char[]  Pointer to string
*/
const char * getClipPath(uint8_t channel, uint8_t clip);

/** @brief  Return number of frames of loaded clip
    @param  channel MIDI channel
    @param  clip Index of clip
    @retval uint32_t Number of frames
*/
uint32_t getClipFrames(uint8_t channel, uint8_t clip);

/** @brief  Return the tempo (BPM) of loaded clip
    @param  channel MIDI channel
    @param  clip Index of clip
    @retval float Tempo in BPM
*/
float getClipTempo(uint8_t channel, uint8_t clip);

/** @brief  Return number of beats of loaded clip
    @param  channel MIDI channel
    @param  clip Index of clip
    @retval uint16_t Number of beats
*/
uint16_t getClipBeats(uint8_t channel, uint8_t clip);

/** @brief  Set clip gain
    @param  channel MIDI channel
    @param  id Clip index
    @param  gain Gain factor (dB)
    @retval uint8_t Error code
*/
uint8_t setGain(uint8_t channel, uint8_t id, float gain);

/** @brief  Get clip gain
    @param  channel MIDI channel
    @param  id Clip index
    @retval float Gain factor (dB)
*/
float getGain(uint8_t channel, uint8_t id);

//!@todo Remove cropping.

/** @brief  Set clip start offset
    @param  channel MIDI channel
    @param  id Clip index
    @param  start Start offset in frames
    @retval uint8_t Error code
*/
uint8_t setStart(uint8_t channel, uint8_t id, uint32_t start);

/** @brief  Get clip start offset
    @param  channel MIDI channel
    @param  id Clip index
    @retval uint32_t Start offset in frames
*/
uint32_t getStart(uint8_t channel, uint8_t id);

/** @brief  Set clip end offset
    @param  channel MIDI channel
    @param  id Clip index
    @param  end End offset in frames
    @retval uint8_t Error code
*/
uint8_t setEnd(uint8_t channel, uint8_t id, uint32_t end);

/** @brief  Get clip end offset
    @param  channel MIDI channel
    @param  id Clip index
    @retval uint32_t End offset in frames
*/
uint32_t getEnd(uint8_t channel, uint8_t id);

/** @brief  Get the samplerate of a file
    @param  path Full path and filename
    @retval uint32_t Samplerate in frames per second or 0z0ero on error
*/
uint32_t getFileSamplerate(const char* path);

/** @brief  Get the quantity of frames in a file
    @param  path Full path and filename
    @retval uint32_t Duration in frames or 0 on error
*/
uint32_t getFileFrames(const char* path);

/** @brief  Arm the recorder to capture audio into a clip slot
    @param  channel MIDI channel
    @param  note MIDI note to trigger clip (1..127)
    @param  channels Quantity of channels to capture (1 or 2)
    @param  tempo Tempo in BPM at time of arming (stored in committed clip)
    @param  max_frames Capture buffer capacity in frames (0 for default MAX_DURATION * samplerate)
    @retval uint8_t Error code
    @note   Only one recording may be armed at a time. Recording starts on
            CLIPPY_VEL_REC_START and commits on CLIPPY_VEL_REC_STOP received
            on the MIDI input port. Poll getRecordState() then call
            saveClip() and disarmRecord().
*/
uint8_t armRecord(uint8_t channel, uint8_t note, uint8_t channels, float tempo, uint32_t max_frames);

/** @brief  Pre-allocate and prefault capture buffers for the next armRecord()
    @param  channels Quantity of channels to prepare (1 or 2)
    @param  max_frames Capture buffer capacity in frames (0 for default MAX_DURATION * samplerate)
    @retval uint8_t Error code (ERROR_EXISTS if another thread holds the prewarm lock)
    @note   Allocating and prefaulting the default capacity takes tens of ms,
            so call this from a background thread after init and after each
            disarmRecord(). armRecord() claims the prepared buffers when the
            size fits (a stereo prewarm also serves a mono take) and falls
            back to allocating inline when it doesn't.
*/
uint8_t prewarmRecordBuffer(uint8_t channels, uint32_t max_frames);

/** @brief  Free any prewarmed capture buffers (called by end())
*/
void freePrewarmBuffer();

/** @brief  Disarm the recorder from any state, freeing uncommitted capture buffers
    @retval uint8_t Error code
    @note   Must be called after REC_DONE (post save), REC_ABORTED or REC_OVERFLOW.
*/
uint8_t disarmRecord();

/** @brief  Set the user latency offset applied to recorded clips
    @param  frames Offset in frames added to the JACK-reported capture latency
                   (may be negative; the combined total is clamped to >= 0)
    @note   Captured audio arrives late by the round-trip latency; the recorder
            compensates by shifting the committed loop region by the combined
            latency, continuing capture (REC_FINISHING) until the tail is full.
*/
void setRecordLatencyOffset(int32_t frames);

/** @brief  Get the user latency offset applied to recorded clips
    @retval int32_t Offset in frames
*/
int32_t getRecordLatencyOffset();

/** @brief  Set whether the recorded player's live input monitor clears at punch-out
    @param  enable 1 to clear the monitor in the RT commit path (AUTO monitor mode)
    @note   Set when arming: avoids the committed loop doubling the live input
            during the gap before the UI updates the monitor state
*/
void setRecordMonitorAuto(uint8_t enable);

/** @brief  Get recorder state
    @retval uint8_t REC_STATE value
*/
uint8_t getRecordState();

/** @brief  Get MIDI channel the recorder is / was armed for
    @retval uint8_t MIDI channel
*/
uint8_t getRecordChannel();

/** @brief  Get MIDI note the recorder is / was armed for
    @retval uint8_t MIDI note
*/
uint8_t getRecordNote();

/** @brief  Get quantity of frames captured so far (or in committed clip)
    @retval uint32_t Quantity of frames
*/
uint32_t getRecordedFrames();

/** @brief  Get quantity of beats captured so far (or in committed clip)
    @retval uint16_t Quantity of beats
*/
uint16_t getRecordedBeats();

/** @brief  Enable / disable live input monitoring for a player
    @param  channel MIDI channel
    @param  enable 1 to mix the player's capture input ports into its output, 0 to disable
    @note   RT-safe flag - takes effect on the next process cycle
*/
void setInputMonitor(uint8_t channel, uint8_t enable);

/** @brief  Get live input monitoring state of a player
    @param  channel MIDI channel
    @retval uint8_t 1 if monitoring enabled
*/
uint8_t getInputMonitor(uint8_t channel);

/** @brief  Set the global monitor routing
    @param  direct 0 to mix monitored inputs into each player's output (default),
                   1 to mix them into the dedicated monitor_a/b output ports
    @note   RT-safe flag - takes effect on the next process cycle
*/
void setMonitorRoute(uint8_t direct);

/** @brief  Get the global monitor routing
    @retval uint8_t 1 if monitoring is routed to the dedicated monitor ports
*/
uint8_t getMonitorRoute();

/** @brief  Set the gain applied to the dedicated monitor output ports
    @param  gain Linear gain (>= 0.0); has no effect when routing through player outputs
    @note   RT-safe - takes effect on the next process cycle
*/
void setMonitorGain(float gain);

/** @brief  Get the gain applied to the dedicated monitor output ports
    @retval float Linear gain
*/
float getMonitorGain();

/** @brief  Save a loaded clip's sample data to a wav file and set its path
    @param  channel MIDI channel
    @param  note MIDI note to trigger clip
    @param  path Full path and filename of file to create
    @retval int Error code (0 on success)
    @note   Blocking (disk I/O) - call from a background thread.
*/
int saveClip(uint8_t channel, uint8_t note, const char* path);

/** @brief  Write sample data to file
    @param  dst_path Full path and filename of file to create
    @param  *data[] Array of deinterleaved (per-channel) sample data
    @param samplerate Sample rate
    @param channels Number of channels in data array
    @param frames Number of frames in data array
    @retval int Error code
*/
int saveFile(const char* dst_path, float *data[], int samplerate, int channels, uint32_t frames);
