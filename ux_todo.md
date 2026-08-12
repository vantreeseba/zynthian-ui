# UI/UX TODO — remaining ideas & research notes

Status of the modernization pass as of 2026-08-12 (branch `ui-modernize`).
Groups A–D are done. From Group E, items E7 (encoder legend strip, with
`ZYNTHIAN_UI_ENCODER_LEGEND` env option + Admin toggle), E8 (pad glyph +
brightness/pulse coding) and E9 (launcher pad progress sweep + pattern
editor off-page playhead arrow) are implemented. Everything below is not
yet done.

## Remaining big ideas

### 10. Meter ballistics
The DPMs currently plot the raw level each refresh. Real console meters
read better because of their time behaviour, not their colours:
- Instant attack (jump up immediately).
- Slow decay (~20 dB per 1.5 s release ramp instead of instant drop).
- Peak-hold marker: thin line at the recent maximum, held ~1.5 s, then
  released. `zynthian_gui_mixer.strip.draw_dpm()` already has a hold
  concept from the mixer engine (`dpm_hold_*`), so this is mostly a GUI
  smoothing layer: keep a per-strip displayed-level state and move it
  toward the target with the asymmetric rates above.

### 11. Performance safety
Live performers need "I can't break anything" guarantees:
- **Temp save / revert checkpoint**: one gesture saves a checkpoint of the
  current snapshot, another reverts to it (Elektron's FUNC+YES temp save /
  FUNC+NO reload is the reference; see research notes). Zynthian already
  has snapshots + a last_state autosave, so this is mostly a pair of CUIA
  actions + a small on-screen confirmation flash.
- **Long-press-confirm-in-place** for destructive actions instead of modal
  confirm screens (hold to arm, release to cancel; ring/progress fill on
  the held control shows the countdown). Push's "hold Delete + tap target"
  chord is the reference.
- **Performance-lock mode**: a toggle that disables edit/destructive
  gestures entirely (pattern clear, chain remove, snapshot overwrite)
  while leaving mixing/launching live.

### 12. Touch ergonomics
On the 800x480 reference screen many targets are below the ~9 mm / ~48 px
minimum:
- Audit: topbar buttons, mixer strip mute/solo zones, selector rows,
  pattern editor cells at default zoom.
- Pattern: **touch-select + encoder-refine** (touch picks the object,
  encoders edit it) rather than trying to make every value draggable —
  this is the MPC touch model and already matches Zynthian's hardware.
- **Relative-drag faders**: a fader should move relative to the finger
  drag delta, never jump to the absolute touch position (Drambo model;
  vertical drag = coarse, horizontal component = fine would be a further
  refinement).

### 13. Contextual hint bar
A one-line hint strip (reuse the encoder-legend canvas slot) that shows
the 2–3 most useful gestures for the current screen state, Polyend
Tracker-style ("HOLD pad = options · BACK = mixer"). Needs a per-screen
hint table; keep it dismissible via the same admin toggle pattern as the
encoder legend.

## Group F (smaller items from the earlier review pass)

- **B7 scroll affordances**: selector listboxes give no hint that more
  rows exist off-screen. Add a slim scrollbar/position pill or top/bottom
  fade when scrollable.
- **B8 value HUD**: while an encoder is turning, show the touched
  parameter name + value large and centered for ~700 ms (helps when the
  controller widget is small or off-screen).
- **B9 modifier legend**: when a switch is held (bold/alt states), show
  what the other switches now do.
- **B10 micro-flash on value commit**: flash the edited widget with
  `color_info` briefly when a value is committed from MIDI/OSC (the
  tempo/BPB flash in the mixer topbar is the in-repo precedent).
- **C11 marquee gating**: `zynthian_gui_base.title_marquee` style scrolls
  run even when text fits; gate on actual overflow measurement.
- **C12 dead-code cleanup**: `zyngui/zynthian_gui_arranger.py` (~1300
  lines) is not registered in `zynthian_gui.py` screens; same for
  `osc_browser` references and `save_preset` leftovers. Verify and remove
  or re-register.

## Items discovered during Group D/E implementation

- **Encoder legend on more screens**: only the mixer opts in via
  `get_zynpot_labels()` because its `update_layout()` ends in
  `build_mixer()` (full rebuild at new height). Selector screens and the
  pattern editor compute geometry once at construction, so opting them in
  requires giving them a real resize path first. See the docstring on
  `zynthian_gui_base.get_zynpot_labels()`.
- **Cued-change display**: launcher rec-countdown already shows punch
  in/out beats in the title (mixer.py, `rec_countdown_clip`). A general
  "queued scene/phrase change" header would need a queued-target concept
  from zynseq follow-actions.
- **euclidseq widget** redraws with `delete("all")` every refresh; keep
  item ids and itemconfig instead (skipped in D5 as it needed a refactor).
- **PAD_COLOUR_EMPTY** pads still read as "grey block"; consider a
  dashed-outline ghost pad + "+" glyph to suggest tap-to-record/assign.
- **`color_blend` callers** assume "#rrggbb" inputs; it now has an
  lru_cache(512). If a caller ever passes Tk named colours it will bypass
  correctness, not just the cache — worth a normalization pass if new
  callers appear.

## Research notes (for future sessions)

Sources: hands-on conventions from groovebox/DAW hardware reviewed during
the redesign research pass.

- **Koala Sampler**: oversized pads, aggressive decluttering — hide
  everything not needed for the current mode; mode switch swaps the whole
  control surface rather than stacking toolbars.
- **Akai MPC touch UIs**: touch selects coarse target, encoders refine.
  Never require precision from the finger; the screen is for *what*, the
  knob is for *how much*.
- **Drambo**: relative drag everywhere; drag axis splits coarse (along
  control) vs fine (perpendicular). No control ever jumps to touch point.
- **Loopy Pro**: finger-count verbs (1-finger = trigger, 2-finger = alt
  action), strict perform-mode vs edit-mode split, radial progress ring
  drawn on the clip itself (inspiration for the pad progress sweep).
- **Ableton Live**: glyph grammar — triangle/square/circle carry meaning,
  colour is decoration only. Any state must survive greyscale. This is
  the principle behind E8's brightness grammar.
- **Novation Launchpad**: animation grammar — static = state, pulse =
  queued, flash = attention; dim = available, bright = active. CVD-safe
  because brightness, not hue, carries the message. E8 implements this.
- **Teenage Engineering OP-1**: encoder colour = position contract; the
  four on-screen colours always map to the four physical encoders in
  order. E7's legend strip should keep cell order == encoder order
  forever, even when a slot is unused.
- **Elektron (Digitakt etc.)**: FUNC+YES temp save / FUNC+NO reload as a
  performance safety net; near-zero screen animation — the screen states
  change, they don't animate, which keeps 10 Hz refresh honest. Page LEDs
  show pattern page + the deferred pattern change is displayed blinking
  until the bar boundary (inspiration for E9 cued-change display).
- **Ableton Push / NI Maschine**: parameter cells physically aligned
  under their encoders + page dots for parameter banks (E7 follow-up if
  the legend gains paging). Push's hold-Delete+tap chord for destructive
  actions (idea 11).
- **Teenage Engineering EP-133**: destructive actions need an explicit
  commit chord (action + commit key), never a single press.
- **Polyend Tracker**: persistent contextual hint line at screen bottom
  (idea 13).
- **Meters** (console convention): instant attack, ~20 dB/1.5 s decay,
  1.5 s peak hold (idea 10).
- **Beat clock**: any beat-synced UI animation should derive from the
  sequencer clock, not a GUI timer — E8's pulse hooks the
  `zynseq.beat` change in `refresh_status` for exactly this reason.
