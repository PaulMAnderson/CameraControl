---
phase: 3
title: Core State Machine — OR Logic, Multi-Source Triggers & OE Stop
context-budget: large
files-required:
  - docs/design-plans/2026-03-27-unified-sync-controller.md
  - camera_capture.py
  - sync_controller.py
depends-on: [phase_01, phase_02]
---

# Unified Sync Controller Implementation Plan

**Goal:** Wire all trigger sources (Matlab UDP, Arduino button, Open Ephys) into a unified "OR Logic" state machine so recording starts on the first enabled source and stops only when the last active source sends a stop signal. Python sends an explicit HTTP STOP to Open Ephys after the video writer closes.

**Architecture:** A `_active_sources` set in `CameraApp` tracks which non-GUI sources have requested recording. `_trigger_start(source)` / `_trigger_stop(source)` centralise the start/stop decision. Open Ephys is intentionally excluded from the stop decision — Python is master and sends OE its stop command after the video file is safely closed.

**Tech Stack:** Python / tkinter (no new dependencies)

**Scope:** Phase 3 of 3 remaining implementation phases

**Codebase verified:** 2026-03-28

---

## Acceptance Criteria Coverage

### unified-sync-controller.AC1: Unified Sync Master
- **unified-sync-controller.AC1.2 Success:** Matlab UDP "START" packet (Port 5005) triggers the internal `start_recording` sequence.
- **unified-sync-controller.AC1.3 Success:** Open Ephys status change to "RECORD" triggers the internal `start_recording` sequence.
- **unified-sync-controller.AC1.4 Failure:** If Arduino is disconnected, GUI shows a prominent error and disables the "Record" button.

### unified-sync-controller.AC2: Multi-Source "OR Logic"
- **unified-sync-controller.AC2.1 Success:** Video PC sends a "STOP" command to Open Ephys HTTP API when the video capture finishes.
- **unified-sync-controller.AC2.2 Success:** Recording only stops when the *last* active source sends a "STOP" command (excluding Ephys, which is a slave to the Video PC's stop command).
- **unified-sync-controller.AC2.3 Success:** Manual GUI "Stop" button overrides any external trigger and ends the recording immediately.

### unified-sync-controller.AC3: Bidirectional Arduino Sync
- **unified-sync-controller.AC3.2 Success:** Pressing the Camera Button starts a synced recording if the GUI is "Armed."
- **unified-sync-controller.AC3.3 Success:** Pressing the Barcode Button toggles the barcode generator on/off independently of video capture.

---

## Note on Testing

This project has no existing automated test infrastructure (no `tests/` directory, no pytest setup). All functionality is hardware-dependent (PySpin camera, Arduino serial, Open Ephys HTTP). Verification is operational — each task below includes explicit manual verification steps using the live hardware or targeted standalone scripts.

---

<!-- START_SUBCOMPONENT_A (tasks 1-2) -->

<!-- START_TASK_1 -->
### Task 1: Add barcode-only commands to `SyncController`

**Verifies:** unified-sync-controller.AC3.3 (prerequisite commands)

**Files:**
- Modify: `sync_controller.py` — add two public methods after `cmd_stop_cam_free` (line 103)

**Implementation:**

```python
def cmd_start_barcodes(self):
    """'B' - start barcode TTLs only, without affecting camera TTLs."""
    self._send(b'B')

def cmd_stop_barcodes(self):
    """'D' - stop barcode TTLs only, without affecting camera TTLs."""
    self._send(b'D')
```

**Verification:**
With Arduino connected and Serial Monitor open:
- Call `sync.cmd_start_barcodes()` → Arduino log shows `Running Barcode TTL Pulses...`, barcode LED starts.
- Call `sync.cmd_stop_barcodes()` → Arduino log shows `Stopping Barcode TTL Pulses...`, barcode LED stops.

**Commit:** `feat(sync): add cmd_start_barcodes / cmd_stop_barcodes public methods`

<!-- END_TASK_1 -->

<!-- START_TASK_2 -->
### Task 2: Wire serial reader and UDP listener in `CameraApp`

**Verifies:** unified-sync-controller.AC1.1, prerequisite for AC1.2, AC3.2, AC3.3

**Files:**
- Modify: `camera_capture.py`

**Changes — three locations:**

**2a. In `__init__` — add new instance variables** after `self._rec_start = None` (line 337):
```python
self._active_sources   = set()    # OR Logic: sources with active start requests
self._barcodes_running = False    # mirrors Arduino barcode state for button toggle
```

**2b. In `__init__` — start new threads** — replace the existing block that calls `self._connect_arduino()` and `self.sync.start_polling(...)` (lines 349–356) with:
```python
print("Connecting Arduino...")
self._connect_arduino()
print("Arduino connect attempted")
if self.sync.arduino_connected:
    self.sync.start_serial_reader(on_arduino_event=self._on_arduino_event)
    print("SerialReaderThread started")
print("Starting OE polling and UDP listener...")
self.sync.start_polling(
    on_record_start=self._oe_record_started,
    on_record_stop=self._oe_record_stopped,
    on_status_change=self._oe_status_changed,
)
self.sync.start_udp_listener(
    on_matlab_start=self._matlab_started,
    on_matlab_stop=self._matlab_stopped,
)
print("OE polling and UDP listener started")
```

**2c. In `_cleanup`** — add UDP listener stop after `self.sync.stop_polling()` (line 832 area). Current:
```python
self.sync.stop_polling()
self.sync.disconnect_arduino()
```
Replace with:
```python
self.sync.stop_polling()
self.sync.stop_udp_listener()
self.sync.disconnect_arduino()   # also stops SerialReaderThread (added in Phase 2)
```

**Verification:**
Launch `camera_capture.py` — check `camera_capture.log` confirms:
- `SerialReaderThread started`
- `OE polling and UDP listener started`
Close the window — no thread-join errors in log.

**Commit:** `feat(app): wire SerialReaderThread and UDPListenerThread into CameraApp lifecycle`

<!-- END_TASK_2 -->

<!-- END_SUBCOMPONENT_A -->

---

<!-- START_SUBCOMPONENT_B (tasks 3-5) -->

<!-- START_TASK_3 -->
### Task 3: Add OR Logic helper methods to `CameraApp`

**Verifies:** unified-sync-controller.AC2.2, AC2.3 (foundation)

**Files:**
- Modify: `camera_capture.py` — add four methods after `_oe_record_stopped` (after line 588)

**Implementation:**

```python
# --------------------------------------------------- OR Logic trigger routing
def _trigger_start(self, source: str):
    """Register a source requesting recording start.
    Starts recording on first call; subsequent calls from other sources just
    add to _active_sources without restarting.
    Only applies in TRIGGERED mode when IDLE or ARMED.
    """
    self._active_sources.add(source)
    if self.state in (AppState.IDLE, AppState.ARMED):
        self._begin_recording()

def _trigger_stop(self, source: str):
    """Register a source releasing its recording request.
    Recording stops only when _active_sources becomes empty.
    OE is intentionally never passed here — Python is OE's master.
    """
    self._active_sources.discard(source)
    if self._active_sources:
        return   # other sources still active, keep recording
    if self.state == AppState.RECORDING:
        self.sync.cmd_recording_ending()
        self._end_recording()

def _on_arduino_event(self, event: str):
    """Handle EVENT: strings from the Arduino SerialReaderThread.
    Called on the tkinter main thread via sync._fire().
    """
    if event == 'CAM_BUTTON':
        if self.state == AppState.ARMED:
            self._trigger_start('button')
        elif self.state == AppState.RECORDING:
            self._trigger_stop('button')
    elif event == 'BARCODE_BUTTON':
        self._toggle_barcodes()
    else:
        print(f"_on_arduino_event: unknown event ignored: {event!r}")

def _toggle_barcodes(self):
    """Toggle barcode pulses on/off independent of video capture (AC3.3)."""
    if self._barcodes_running:
        self.sync.cmd_stop_barcodes()
        self._barcodes_running = False
    else:
        self.sync.cmd_start_barcodes()
        self._barcodes_running = True
```

**Verification:**
With Arduino connected and firmware from Phase 1:
- Press barcode button → barcodes toggle on/off independent of recording.
- When ARMED, press cam button → recording starts.
- When RECORDING, press cam button → recording stops (if button is only active source).

**Commit:** `feat(app): add _trigger_start/_trigger_stop OR Logic and Arduino event routing`

<!-- END_TASK_3 -->

<!-- START_TASK_4 -->
### Task 4: Add Matlab UDP callbacks and update OE callbacks

**Verifies:** unified-sync-controller.AC1.2, AC1.3, AC2.2, AC2.3

**Files:**
- Modify: `camera_capture.py`

**4a. Add Matlab callbacks** — add after `_toggle_barcodes`:
```python
def _matlab_started(self):
    """Matlab sent a UDP 'START' — request recording start (TRIGGERED mode only)."""
    if self.mode != CaptureMode.TRIGGERED:
        return
    self._trigger_start('matlab')

def _matlab_stopped(self):
    """Matlab sent a UDP 'STOP' — release matlab's recording request."""
    self._trigger_stop('matlab')
```

**4b. Update `_oe_record_started`** (lines 575–581) — replace body to use `_trigger_start`:
```python
def _oe_record_started(self):
    """Open Ephys just started recording — trigger capture if in TRIGGERED mode."""
    if self.mode != CaptureMode.TRIGGERED:
        return
    if self.state not in (AppState.IDLE, AppState.ARMED):
        return
    self._trigger_start('oe')
```

**4c. Update `_oe_record_stopped`** (lines 583–588) — OE stop is now intentionally ignored; Python sends stop to OE after video finishes (AC2.1):
```python
def _oe_record_stopped(self):
    """Open Ephys stopped recording.
    Intentional no-op: Python is OE's master.
    The video continues until the last active source (Matlab or button) stops.
    Python sends the STOP command to OE in _finish_worker after the writer closes.
    """
    pass
```

**4d. Update `_on_stop`** (lines 619–634) — clear `_active_sources` first (AC2.3 manual override):
```python
def _on_stop(self):
    self._active_sources.clear()   # manual override: cancel all external requests
    if self.state == AppState.ARMED:
        self.sync.cmd_recording_ending()
        self._barcodes_running = False
        self.state = AppState.IDLE
        self._set_state_label("● IDLE", 'grey')
        self.record_btn.config(state='normal')
        self.stop_btn.config(state='disabled')
        return

    if self.state == AppState.RECORDING:
        if self.mode == CaptureMode.FREE_RECORD:
            self.sync.cmd_stop_cam_free()
        elif self.mode == CaptureMode.TRIGGERED:
            self.sync.cmd_recording_ending()
            self._barcodes_running = False   # 'X' stops barcodes on Arduino
        self._end_recording()
```

**4e. Remove redundant `_oe_record_started()` call from `_oe_status_changed`** (lines 571–573) — the OE poll thread's `on_record_start` callback already calls it; calling it again from `_oe_status_changed` creates a double-dispatch that adds `'oe'` twice (harmless but confusing). Remove the conditional at the bottom of `_oe_status_changed`:

Current (lines 571–573):
```python
    # In triggered mode, ARMED state means we're waiting for OE to start
    if self.state == AppState.ARMED and state == 'RECORD':
        self._oe_record_started()
```

Remove these three lines entirely. The `start_polling(on_record_start=self._oe_record_started, ...)` callback registered in `__init__` is the single authoritative dispatch path.

**Verification:**
- Send UDP `START` → recording begins (if ARMED).
- Send UDP `STOP` → recording stops (if matlab is last active source).
- OE starts recording → capture starts (fired exactly once from polling callback). OE stops → capture continues until manual stop or Matlab stop.

**Commit:** `feat(app): add Matlab callbacks, make OE stop a no-op (Python is OE master)`

<!-- END_TASK_4 -->

<!-- START_TASK_5 -->
### Task 5: Send OE STOP after video writer closes

**Verifies:** unified-sync-controller.AC2.1

**Files:**
- Modify: `camera_capture.py` — update `_finish_worker` (lines 708–745)

**Implementation:**

Add the OE stop call after `write_metadata(self._filepath, meta)` and before restarting the preview. Locate the line `write_metadata(self._filepath, meta)` (around line 737) and add immediately after it:

```python
        # Tell Open Ephys to stop recording — Python is master, OE is slave (AC2.1)
        # Called after writer closes so the ephys file captures the final barcode
        self.sync.cmd_stop_oe_recording()
```

The complete updated sequence in `_finish_worker` from `write_metadata` onward:
```python
        write_metadata(self._filepath, meta)

        # Tell Open Ephys to stop recording — Python is master, OE is slave (AC2.1)
        self.sync.cmd_stop_oe_recording()

        # Restart preview-only capture
        print("_finish_worker: restarting preview capture...")
        self._start_preview_capture()
        print("_finish_worker: preview restarted OK")

        # Back to GUI thread
        self.root.after(0, self._recording_finished)
```

**Also:** reset `_barcodes_running` flag in `_recording_finished` to stay in sync with Arduino state after 'X' is sent:
```python
def _recording_finished(self):
    self._barcodes_running = False   # 'X' command stops barcodes on recording end
    self.state = AppState.IDLE
    self._set_state_label("● IDLE", 'grey')
    self.record_btn.config(state='normal')
    self.stop_btn.config(state='disabled')
    self.animal_entry.config(state='normal')
    saved = self._frame_stats.get('saved', 0)
    dropped = self._frame_stats.get('dropped', 0)
    self._file_label.config(
        text=f"Saved: {Path(self._filepath).name}  ({saved} frames, {dropped} dropped)",
        fg='#4CAF50' if dropped == 0 else '#FF9800'
    )
```

**Verification:**
1. Have Open Ephys running.
2. Press Record (TRIGGERED mode) — ARM the system.
3. Start recording in OE → capture begins.
4. Press Stop in GUI → video finishes → check OE returns to IDLE state automatically.
5. Check `camera_capture.log` — no exceptions in `_finish_worker`.

**Commit:** `feat(app): send OE HTTP STOP after video writer closes (AC2.1)`

<!-- END_TASK_5 -->

<!-- END_SUBCOMPONENT_B -->

---

## End-of-Phase Verification

After all five tasks are complete, run through the full integration test:

**Single-source OE trigger (existing behaviour preserved):**
1. TRIGGERED mode, press Record → ARMED state.
2. OE starts recording → camera capture starts, `● RECORDING` shown.
3. Press GUI Stop → video closes, OE returns to IDLE, metadata marked `complete`.

**Multi-source OR Logic (new behaviour):**
4. TRIGGERED mode, press Record → ARMED.
5. Send Matlab UDP `START` → recording starts, source set = `{matlab}`.
6. OE starts independently → source set = `{matlab, oe}`.
7. OE stops → source set still `{matlab}`, recording continues.
8. Send Matlab UDP `STOP` → source set = `{}`, recording stops, OE sent STOP.

**Physical button (new behaviour):**
9. TRIGGERED mode, press Record → ARMED.
10. Press physical cam button → recording starts, source set = `{button}`.
11. Press cam button again → recording stops.

**Barcode button (new behaviour):**
12. Press barcode button at any time → barcodes toggle on; press again → barcodes toggle off.
13. Barcode state is independent of video recording state.

**Manual override (AC2.3):**
14. With Matlab and button both active, press GUI Stop → all sources cleared, recording stops immediately.
