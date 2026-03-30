# Test Requirements: Unified Sync Controller

**Design:** `docs/design-plans/2026-03-27-unified-sync-controller.md`
**Date:** 2026-03-28
**Test infrastructure:** None (no pytest, no test directory). All verification is manual against live hardware (FLIR camera via PySpin, Arduino serial, Open Ephys HTTP).

---

## Table of Contents

- [AC1: Unified Sync Master](#ac1-unified-sync-master)
  - [AC1.1 Background threads start without crashing](#ac11-background-threads-start-without-crashing)
  - [AC1.2 Matlab UDP START triggers recording](#ac12-matlab-udp-start-triggers-recording)
  - [AC1.3 Open Ephys RECORD triggers recording](#ac13-open-ephys-record-triggers-recording)
  - [AC1.4 Arduino disconnect shows error and disables Record](#ac14-arduino-disconnect-shows-error-and-disables-record)
- [AC2: Multi-Source OR Logic](#ac2-multi-source-or-logic)
  - [AC2.1 Video PC sends STOP to Open Ephys after capture finishes](#ac21-video-pc-sends-stop-to-open-ephys-after-capture-finishes)
  - [AC2.2 Recording stops only when last active source stops](#ac22-recording-stops-only-when-last-active-source-stops)
  - [AC2.3 Manual GUI Stop overrides all external triggers](#ac23-manual-gui-stop-overrides-all-external-triggers)
- [AC3: Bidirectional Arduino Sync](#ac3-bidirectional-arduino-sync)
  - [AC3.1 Buttons emit EVENT serial strings](#ac31-buttons-emit-event-serial-strings)
  - [AC3.2 Camera Button starts synced recording when Armed](#ac32-camera-button-starts-synced-recording-when-armed)
  - [AC3.3 Barcode Button toggles barcodes independently](#ac33-barcode-button-toggles-barcodes-independently)
- [AC4: Fail-Safe Metadata (EXISTING)](#ac4-fail-safe-metadata-existing)
  - [AC4.1 JSON sidecar created within 500ms of first frame](#ac41-json-sidecar-created-within-500ms-of-first-frame)
  - [AC4.2 Clean shutdown updates metadata to complete](#ac42-clean-shutdown-updates-metadata-to-complete)
  - [AC4.3 Dropped frame count matches camCapture counter](#ac43-dropped-frame-count-matches-camcapture-counter)
- [AC5: Mode-Specific Behavior (EXISTING)](#ac5-mode-specific-behavior-existing)
  - [AC5.1 View Only shows preview without TTL or barcode activity](#ac51-view-only-shows-preview-without-ttl-or-barcode-activity)
  - [AC5.2 Free Record sends camera pulses at software-defined framerate](#ac52-free-record-sends-camera-pulses-at-software-defined-framerate)

---

## AC1: Unified Sync Master

### AC1.1 Background threads start without crashing

> **AC text (verbatim):** `SyncController` starts background threads for Serial, UDP, and HTTP polling without crashing.

**Test type:** manual-hardware

**Introduced by:** Phase 2, Tasks 2-3 (SerialReaderThread, UDPListenerThread); OE polling already exists.

**Verification procedure:**

1. Ensure Arduino is connected via USB and Open Ephys is running (or at least reachable on the configured host/port).
2. Launch `camera_capture.py`.
3. Wait for the GUI to appear (approximately 5-10 seconds).
4. Open `camera_capture.log` in a text editor or terminal.
5. Search the log for the following three lines:
   - `SerialReaderThread started`
   - `OE polling and UDP listener started`
   - No `Exception` or `Traceback` entries in the log.
6. Leave the application running for 30 seconds. Confirm no thread-crash messages appear.
7. Close the application via the window close button. Confirm no `thread.join` timeout errors in the log.

**Pass criteria:** All three thread start messages are present in `camera_capture.log`, no exceptions are logged during startup or the 30-second idle period, and the application shuts down cleanly without join errors.

---

### AC1.2 Matlab UDP START triggers recording

> **AC text (verbatim):** Matlab UDP "START" packet (Port 5005) triggers the internal `start_recording` sequence.

**Test type:** manual-script

**Introduced by:** Phase 2, Task 3 (UDP transport); Phase 3, Task 4 (routing to `_trigger_start`).

**Verification procedure:**

1. Launch `camera_capture.py` with Arduino connected and camera available.
2. Select **Triggered** mode from the mode selector.
3. Enter an animal name and press **Record**. GUI should show `ARMED` state.
4. From a separate terminal on the same machine, run:
   ```bash
   python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'START', ('127.0.0.1', 5005))"
   ```
5. Observe the GUI transitions from `ARMED` to `RECORDING`.
6. Verify frames are being captured (frame counter in GUI increments).
7. Send stop:
   ```bash
   python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'STOP', ('127.0.0.1', 5005))"
   ```
8. Observe recording stops and GUI returns to `IDLE`.

**Pass criteria:** The GUI transitions ARMED -> RECORDING upon receiving the UDP START packet, frame counter increments during recording, and the GUI returns to IDLE after the UDP STOP packet. A video file and `.json` sidecar are created in the output directory.

---

### AC1.3 Open Ephys RECORD triggers recording

> **AC text (verbatim):** Open Ephys status change to "RECORD" triggers the internal `start_recording` sequence.

**Test type:** manual-hardware

**Introduced by:** Phase 3, Task 4 (updated `_oe_record_started` routing through `_trigger_start`). OE polling thread is pre-existing.

**Verification procedure:**

1. Launch `camera_capture.py` with Arduino connected and camera available.
2. Ensure Open Ephys is running and in ACQUIRE or IDLE mode.
3. Select **Triggered** mode, enter an animal name, press **Record**. GUI shows `ARMED`.
4. In Open Ephys, click the Record button.
5. Within the OE poll interval (default 500ms), the camera GUI should transition from `ARMED` to `RECORDING`.
6. Verify frames are being captured (frame counter increments).
7. Press **Stop** in the camera GUI (do not stop OE directly -- Python is master).
8. Verify OE is sent back to IDLE automatically (tested under AC2.1).

**Pass criteria:** The camera GUI transitions from ARMED to RECORDING within 1 second of Open Ephys starting to record. Frame counter increments confirm active capture.

---

### AC1.4 Arduino disconnect shows error and disables Record

> **AC text (verbatim):** If Arduino is disconnected, GUI shows a prominent error and disables the "Record" button.

**Test type:** manual-hardware

**Introduced by:** Phase 3, Task 2 (wiring); error handling is existing application behavior extended by the `SerialReaderThread` `SerialException` handler in Phase 2, Task 2.

**Verification procedure:**

1. Launch `camera_capture.py` with Arduino connected. Confirm normal startup (no errors, Record button enabled).
2. Physically unplug the Arduino USB cable.
3. Wait up to 5 seconds for the serial reader thread to detect the disconnection.
4. Observe:
   - The GUI displays a prominent error message or status indicator showing the Arduino is disconnected.
   - The **Record** button becomes disabled (greyed out).
5. Attempt to click the Record button -- it should not respond.
6. Reconnect the Arduino and restart the application to confirm recovery.

**Pass criteria:** Within 5 seconds of USB disconnection, the GUI shows a visible error state and the Record button is disabled. The button cannot be clicked while in error state.

---

## AC2: Multi-Source OR Logic

### AC2.1 Video PC sends STOP to Open Ephys after capture finishes

> **AC text (verbatim):** Video PC sends a "STOP" command to Open Ephys HTTP API when the video capture finishes.

**Test type:** manual-hardware

**Introduced by:** Phase 2, Task 4 (`cmd_stop_oe_recording` method); Phase 3, Task 5 (call site in `_finish_worker`).

**Verification procedure:**

1. Launch `camera_capture.py` with Arduino connected and camera available.
2. Ensure Open Ephys is running and in ACQUIRE mode.
3. Select **Triggered** mode, enter an animal name, press **Record** (ARMED).
4. Start recording in Open Ephys. Camera GUI should transition to RECORDING.
5. Press **Stop** in the camera GUI.
6. Wait for the video file to finish writing (GUI returns to IDLE, file label shows saved filename).
7. Check Open Ephys -- it should have automatically returned to IDLE or ACQUIRE state (no longer recording).
8. Verify in `camera_capture.log` there is no exception from `cmd_stop_oe_recording`.

**Pass criteria:** After the camera GUI finishes writing the video and returns to IDLE, Open Ephys has also stopped recording and returned to a non-RECORD state, without manual intervention in OE.

---

### AC2.2 Recording stops only when last active source stops

> **AC text (verbatim):** Recording only stops when the *last* active source sends a "STOP" command (excluding Ephys, which is a slave to the Video PC's stop command).

**Test type:** manual-script

**Introduced by:** Phase 3, Tasks 3-4 (`_trigger_start`/`_trigger_stop` OR Logic, Matlab callbacks).

**Verification procedure:**

1. Launch `camera_capture.py` with Arduino connected, camera available, and Open Ephys running.
2. Select **Triggered** mode, enter an animal name, press **Record** (ARMED).
3. Send Matlab UDP START:
   ```bash
   python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'START', ('127.0.0.1', 5005))"
   ```
4. Confirm recording starts. Active source set = `{matlab}`.
5. Start recording in Open Ephys. Active source set = `{matlab, oe}`.
6. Stop recording in Open Ephys. Recording should **continue** (Matlab is still active, and OE stop is intentionally ignored by Python).
7. Send Matlab UDP STOP:
   ```bash
   python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'STOP', ('127.0.0.1', 5005))"
   ```
8. Recording should now stop (source set is empty).

**Alternative test with button + Matlab:**

1. ARMED state.
2. Press physical cam button on Arduino -> recording starts, source = `{button}`.
3. Send Matlab UDP START -> source = `{button, matlab}`.
4. Press cam button again -> source = `{matlab}`, recording continues.
5. Send Matlab UDP STOP -> source = `{}`, recording stops.

**Pass criteria:** Recording persists as long as at least one non-OE source remains active. Recording stops only when the last source sends its stop signal. The frame counter continues incrementing through intermediate stop signals from individual sources.

---

### AC2.3 Manual GUI Stop overrides all external triggers

> **AC text (verbatim):** Manual GUI "Stop" button overrides any external trigger and ends the recording immediately.

**Test type:** manual-script

**Introduced by:** Phase 3, Task 4 (`_on_stop` clears `_active_sources`).

**Verification procedure:**

1. Launch `camera_capture.py` with Arduino connected and camera available.
2. Select **Triggered** mode, enter an animal name, press **Record** (ARMED).
3. Activate multiple sources:
   - Send Matlab UDP START.
   - Press Arduino cam button (if ARMED, this also triggers start; if already recording, it adds to active set).
4. Confirm recording is active with multiple sources.
5. Press the **Stop** button in the GUI.
6. Recording should stop **immediately** regardless of active external sources.
7. Verify GUI returns to IDLE.
8. Verify the video file and metadata sidecar are written correctly.

**Pass criteria:** Pressing the GUI Stop button ends the recording immediately. The GUI returns to IDLE. No further frames are captured after the Stop button is pressed. The `_active_sources` set is cleared (observable via log output or by the fact that no stale stop signals cause errors later).

---

## AC3: Bidirectional Arduino Sync

### AC3.1 Buttons emit EVENT serial strings

> **AC text (verbatim):** External "Barcode Button" and "Camera Button" trigger separate `EVENT:BARCODE_BUTTON` and `EVENT:CAM_BUTTON` messages.

**Test type:** manual-hardware

**Introduced by:** Phase 1, Tasks 1-2 (Arduino firmware refactor).

**Verification procedure:**

1. Upload the Phase 1 firmware to the Arduino.
2. Open Arduino Serial Monitor at 9600 baud.
3. Press the **barcode button** on the hardware.
4. Observe Serial Monitor output: `EVENT:BARCODE_BUTTON`.
5. Confirm barcode TTL pulses do NOT start (check oscilloscope or LED indicator on barcode output pin).
6. Press the **camera button** on the hardware.
7. Observe Serial Monitor output: `EVENT:CAM_BUTTON`.
8. Confirm camera TTL pulses do NOT start.
9. Send `R` command via Serial Monitor -> barcodes and camera pulses start as expected (commands still work).
10. Send `X` -> pulses stop.
11. Power-cycle Arduino, verify all pins are LOW at boot.

**Pass criteria:** Each button press produces exactly the corresponding `EVENT:` string on Serial. Neither button directly toggles any TTL output. Serial commands (`R`, `X`, `B`, `D`, `C`, `E`) continue to function correctly. All pins are LOW after power-cycle.

---

### AC3.2 Camera Button starts synced recording when Armed

> **AC text (verbatim):** Pressing the Camera Button starts a synced recording if the GUI is "Armed."

**Test type:** manual-hardware

**Introduced by:** Phase 1, Task 2 (EVENT emission); Phase 2, Task 2 (serial reader); Phase 3, Task 3 (`_on_arduino_event` routing).

**Verification procedure:**

1. Launch `camera_capture.py` with Arduino connected and camera available.
2. Select **Triggered** mode, enter an animal name, press **Record**. GUI shows `ARMED`.
3. Press the **physical camera button** on the Arduino.
4. Observe the GUI transitions from `ARMED` to `RECORDING`.
5. Verify frames are being captured (frame counter increments).
6. Press the **physical camera button** again.
7. Recording should stop (button is the only active source, pressing again calls `_trigger_stop('button')`).
8. GUI returns to IDLE.
9. Verify the video file and metadata sidecar are created.

**Additional negative test:**

10. With GUI in IDLE (not ARMED), press the camera button. Nothing should happen -- no recording starts, no errors in log.

**Pass criteria:** In ARMED state, a physical camera button press starts recording. In RECORDING state with button as the only source, a second press stops recording. In IDLE state, button presses are ignored. No exceptions in `camera_capture.log`.

---

### AC3.3 Barcode Button toggles barcodes independently

> **AC text (verbatim):** Pressing the Barcode Button toggles the barcode generator on/off independently of video capture.

**Test type:** manual-hardware

**Introduced by:** Phase 1, Task 1 (EVENT emission); Phase 2, Task 2 (serial reader); Phase 3, Tasks 1 and 3 (`cmd_start_barcodes`/`cmd_stop_barcodes`, `_toggle_barcodes`).

**Verification procedure:**

1. Launch `camera_capture.py` with Arduino connected.
2. With the GUI in IDLE state (no recording), press the **physical barcode button**.
3. Verify barcode TTL pulses start (check oscilloscope or LED indicator). The `_barcodes_running` flag is now `True`.
4. Press the barcode button again.
5. Verify barcode TTL pulses stop. The `_barcodes_running` flag is now `False`.
6. Start a recording (any mode).
7. During recording, press the barcode button. Barcodes should toggle independently of the video capture state.
8. Stop the recording. Verify the barcode state persists (if they were on, they remain on; if off, they remain off) -- except in Triggered mode where the `X` command resets barcodes.

**Pass criteria:** Barcode button toggles barcode TTL output on/off each press. The toggle works in IDLE, ARMED, and RECORDING states. Barcode state is independent of video recording state (with the noted exception that the `X` stop-sync command resets barcodes in Triggered mode).

---

## AC4: Fail-Safe Metadata (EXISTING)

> **Status: EXISTING -- verify not regressed.** Phase 4 is already implemented. These tests confirm the existing behavior is preserved after Phases 1-3 are applied.

### AC4.1 JSON sidecar created within 500ms of first frame

> **AC text (verbatim):** A `.json` file is created within 500ms of the first frame being captured, containing `"status": "recording"`.

**Test type:** manual-hardware

**Introduced by:** Phase 4 (already complete). Verify not regressed by Phase 3 changes to `_begin_recording`.

**Verification procedure:**

1. Launch `camera_capture.py` with Arduino connected and camera available.
2. Note the configured output directory from `config.json`.
3. Open a file manager or terminal watching the output directory (`ls -lt` or equivalent).
4. Start a recording (any mode -- Free Record is simplest for this test).
5. Within 1 second of pressing Record, check for a new `.json` file in the output directory.
6. Open the `.json` file immediately (before stopping the recording).
7. Verify it contains `"status": "recording"`.
8. Verify it contains the animal name, date, and other expected metadata fields.

**Pass criteria:** A `.json` sidecar file appears in the output directory within 1 second of recording start. The file contains `"status": "recording"` when read during an active recording session.

---

### AC4.2 Clean shutdown updates metadata to complete

> **AC text (verbatim):** On clean shutdown, the metadata file is updated with final frame counts and `"status": "complete"`.

**Test type:** manual-hardware

**Introduced by:** Phase 4 (already complete). Verify not regressed by Phase 3 changes to `_finish_worker`.

**Verification procedure:**

1. Start a recording (any mode). Let it run for at least 5 seconds to accumulate frames.
2. Press **Stop** in the GUI. Wait for the GUI to return to IDLE and show the saved file summary.
3. Open the `.json` sidecar file for the recording that just completed.
4. Verify the following fields:
   - `"status": "complete"`
   - `"frames_saved"` is a positive integer matching (approximately) the frame count shown in the GUI.
   - `"frames_dropped"` is present (may be 0).
5. Verify the video file exists and is non-zero size.

**Pass criteria:** The `.json` sidecar contains `"status": "complete"`, a positive `frames_saved` count, and a `frames_dropped` count. The values are consistent with the GUI's displayed summary.

---

### AC4.3 Dropped frame count matches camCapture counter

> **AC text (verbatim):** Dropped frame count in metadata matches the `camCapture` dropped frame counter exactly.

**Test type:** manual-hardware

**Introduced by:** Phase 4 (already complete). Verify not regressed.

**Verification procedure:**

1. Start a recording in **Free Record** mode at a high framerate (e.g., 60fps or higher) to increase the chance of dropped frames, or in **Triggered** mode at 400Hz.
2. Let the recording run for at least 30 seconds.
3. Press **Stop**. Note the frame count summary displayed in the GUI (e.g., `"150 frames, 2 dropped"`).
4. Open the `.json` sidecar file.
5. Compare `"frames_dropped"` in the JSON to the dropped count shown in the GUI.
6. Check `camera_capture.log` for any `dropped frame` log entries and compare the count.

**Pass criteria:** The `frames_dropped` value in the JSON sidecar matches exactly the dropped frame count displayed in the GUI summary label, which in turn matches the internal `camCapture` dropped frame counter logged to `camera_capture.log`.

---

## AC5: Mode-Specific Behavior (EXISTING)

> **Status: EXISTING -- verify not regressed.** Phase 5 is already implemented. These tests confirm the existing behavior is preserved after Phases 1-3 are applied.

### AC5.1 View Only shows preview without TTL or barcode activity

> **AC text (verbatim):** "View Only" mode shows a preview but **must not** trigger camera pulses or barcodes.

**Test type:** manual-hardware

**Introduced by:** Phase 5 (already complete). Verify not regressed.

**Verification procedure:**

1. Launch `camera_capture.py` with Arduino connected and camera available.
2. Select **View Only** mode from the mode selector.
3. Verify the camera preview is displayed in the GUI (live image updates at approximately 15fps).
4. Monitor the Arduino TTL output pins with an oscilloscope or LED indicators.
5. Verify:
   - No camera TTL pulses are being generated.
   - No barcode TTL pulses are being generated.
6. Attempt to press **Record** -- the application should show a messagebox or otherwise prevent recording, and no file is written to disk.
7. Check the output directory -- no new video files or `.json` sidecars should have been created.
8. Remain in View Only mode for 30 seconds. Confirm the preview continues and no TTL activity occurs.

**Pass criteria:** The camera preview is visible and updating. No TTL pulses (camera or barcode) are generated on any Arduino output pin. No video files or metadata files are written to disk. The Record button either shows a warning or is logically prevented from starting a capture.

---

### AC5.2 Free Record sends camera pulses at software-defined framerate

> **AC text (verbatim):** "Free Record" mode sends camera pulses synchronized to the software-defined framerate.

**Test type:** manual-hardware

**Introduced by:** Phase 5 (already complete). Verify not regressed.

**Verification procedure:**

1. Launch `camera_capture.py` with Arduino connected and camera available.
2. Select **Free Record** mode from the mode selector.
3. Verify the FPS combobox becomes enabled. Select a framerate (e.g., 30fps).
4. Enter an animal name and press **Record**.
5. Monitor the Arduino camera TTL output with an oscilloscope.
6. Verify:
   - Camera TTL pulses are being generated at approximately the selected framerate (e.g., ~33ms period for 30fps).
   - The GUI shows `RECORDING` state with an incrementing frame counter.
7. Press **Stop**. Verify:
   - Camera TTL pulses stop.
   - The GUI returns to IDLE.
   - A video file and `.json` sidecar are created.
8. Open the `.json` sidecar and verify the framerate field matches the selected value.

**Pass criteria:** Camera TTL pulses are generated at a frequency matching the user-selected framerate (within 5% tolerance measured on oscilloscope). Frames are captured and saved. The metadata reflects the correct framerate setting.

---

## Summary Matrix

| AC | Test Type | Introduced By | Status |
|----|-----------|---------------|--------|
| AC1.1 | manual-hardware | Phase 2, Tasks 2-3 | New |
| AC1.2 | manual-script | Phase 2 Task 3 + Phase 3 Task 4 | New |
| AC1.3 | manual-hardware | Phase 3, Task 4 | New |
| AC1.4 | manual-hardware | Phase 2 Task 2 + Phase 3 Task 2 | New |
| AC2.1 | manual-hardware | Phase 2 Task 4 + Phase 3 Task 5 | New |
| AC2.2 | manual-script | Phase 3, Tasks 3-4 | New |
| AC2.3 | manual-script | Phase 3, Task 4 | New |
| AC3.1 | manual-hardware | Phase 1, Tasks 1-2 | New |
| AC3.2 | manual-hardware | Phase 1 + Phase 2 + Phase 3 Task 3 | New |
| AC3.3 | manual-hardware | Phase 1 + Phase 2 + Phase 3 Tasks 1,3 | New |
| AC4.1 | manual-hardware | Phase 4 (complete) | EXISTING -- verify not regressed |
| AC4.2 | manual-hardware | Phase 4 (complete) | EXISTING -- verify not regressed |
| AC4.3 | manual-hardware | Phase 4 (complete) | EXISTING -- verify not regressed |
| AC5.1 | manual-hardware | Phase 5 (complete) | EXISTING -- verify not regressed |
| AC5.2 | manual-hardware | Phase 5 (complete) | EXISTING -- verify not regressed |
