# Unified Sync Controller Design

## Summary
The Unified Sync Controller is a centralized orchestration system designed to synchronize high-speed camera capture with external experimental triggers from Matlab, Open Ephys, and physical hardware buttons. At its core, a Python-based master application manages a multi-threaded state machine that listens for "START" and "STOP" signals across UDP, HTTP, and Serial interfaces. By implementing an "OR Logic" triggering strategy, the system allows any enabled source to initiate a recording session, ensuring that video data is perfectly aligned with electrophysiology and behavioral data streams.

The implementation relies on a "Slave Pulse Generator" architecture where an Arduino handles the precision timing of TTL pulses for camera triggering and barcode generation. This separation of concerns allows the Python application to handle high-level logic and GUI interactions while the Arduino ensures hardware-level temporal accuracy. Robustness is a primary focus, with fail-safe metadata generation that records the session status immediately upon startup and updates with frame-accurate statistics upon a clean exit.

## 🗂 Memory Tier Index

| Tier | Sections | When to read |
|------|----------|-------------|
| 🔴 **HOT** | DoD, Acceptance Criteria | Always — load before doing anything |
| 🟡 **WARM** | Architecture, Existing Patterns, Implementation Phases | When planning or implementing a specific phase |
| 🔵 **COOLD** | Glossary, Additional Considerations | Reference on demand only |

## 🔴 Definition of Done
1. **Unified Sync Master:** A Python `SyncController` class managing Arduino (USB/Serial), Open Ephys (HTTP Polling), and Matlab (UDP) triggers in a single thread.
2. **Robust Multi-Mode GUI:** A single-window, console-less GUI with:
   - **View Only:** 15fps preview, no disk I/O.
   - **Free Record:** Software-timed capture (user-defined FPS), immediate saving.
   - **Synced Record:** Hardware-triggered capture (400Hz), armed by a configurable source (Ephys, Matlab, or Manual Button).
3. **Bidirectional Arduino Sync:** Arduino code modified to:
   - Default all TTL outputs (barcodes, camera pulses) to LOW on startup.
   - Report physical "Barcode" and "Camera" button presses back to Python via Serial.
   - Handle 'R'/'X' (Recording Active/End) and 'C'/'E' (Free Run/End) commands.
4. **Fail-Safe Metadata:** Immediate generation of a `.json` sidecar on start ("recording" status) and updated on clean exit ("complete" status, frame counts).
5. **Configurable Rig Portability:** All per-machine settings (IPs, Ports, Paths, GPU Codecs) abstracted into a `config.json`.

## 🔴 Acceptance Criteria

### unified-sync-controller.AC1: Unified Sync Master
- **unified-sync-controller.AC1.1 Success:** `SyncController` starts background threads for Serial, UDP, and HTTP polling without crashing.
- **unified-sync-controller.AC1.2 Success:** Matlab UDP "START" packet (Port 5005) triggers the internal `start_recording` sequence.
- **unified-sync-controller.AC1.3 Success:** Open Ephys status change to "RECORD" triggers the internal `start_recording` sequence.
- **unified-sync-controller.AC1.4 Failure:** If Arduino is disconnected, GUI shows a prominent error and disables the "Record" button.

### unified-sync-controller.AC2: Multi-Source "OR Logic"
- **unified-sync-controller.AC2.1 Success:** Video PC sends a "STOP" command to Open Ephys HTTP API when the video capture finishes.
- **unified-sync-controller.AC2.2 Success:** Recording only stops when the *last* active source sends a "STOP" command (excluding Ephys, which is a slave to the Video PC's stop command).
- **unified-sync-controller.AC2.3 Success:** Manual GUI "Stop" button overrides any external trigger and ends the recording immediately.

### unified-sync-controller.AC3: Bidirectional Arduino Sync
- **unified-sync-controller.AC3.1 Success:** External "Barcode Button" and "Camera Button" trigger separate `EVENT:BARCODE_BUTTON` and `EVENT:CAM_BUTTON` messages.
- **unified-sync-controller.AC3.2 Success:** Pressing the Camera Button starts a synced recording if the GUI is "Armed."
- **unified-sync-controller.AC3.3 Success:** Pressing the Barcode Button toggles the barcode generator on/off independently of video capture.

### unified-sync-controller.AC4: Fail-Safe Metadata
- **unified-sync-controller.AC4.1 Success:** A `.json` file is created within 500ms of the first frame being captured, containing `"status": "recording"`.
- **unified-sync-controller.AC4.2 Success:** On clean shutdown, the metadata file is updated with final frame counts and `"status": "complete"`.
- **unified-sync-controller.AC4.3 Success:** Dropped frame count in metadata matches the `camCapture` dropped frame counter exactly.

### unified-sync-controller.AC5: Mode-Specific Behavior
- **unified-sync-controller.AC5.1 Success:** "View Only" mode shows a preview but **must not** trigger camera pulses or barcodes.
- **unified-sync-controller.AC5.2 Success:** "Free Record" mode sends camera pulses synchronized to the software-defined framerate.

## 🔵 Glossary
- **TTL (Transistor-Transistor Logic)**: A standard for digital signals where specific voltage levels represent binary states, used here for precision hardware synchronization of cameras and timestamps.
- **Barcode**: A unique, time-varying sequence of TTL pulses emitted by the Arduino to provide a physical "timestamp" within the video and ephys data for post-hoc alignment.
- **Open Ephys**: An open-source electrophysiology acquisition platform that provides its current recording status via an internal HTTP-based API.
- **OR Logic**: A triggering mechanism where a recording starts if *any* enabled source (Matlab UDP, Open Ephys HTTP, or Manual Button) sends a start signal.
- **UDP (User Datagram Protocol)**: A fast, connectionless network protocol used to receive low-latency triggers from Matlab behavioral scripts over a local network.
- **Sidecar File**: A JSON metadata file created alongside the video data to store essential capture settings, frame counts, and synchronization status.
- **Armed**: A software state indicating the system is actively waiting for an external hardware or network trigger to begin a Synced Record session.
- **Free Record**: A capture mode where the camera is triggered by software timers at a user-defined framerate, independent of hardware sync pulses.
- **Synced Record**: A capture mode where each frame is triggered by a physical TTL pulse from the Arduino, ensuring sub-millisecond alignment with external hardware.

## 🟡 Architecture

The system follows a **Centralized Hub** architecture where the Video PC (Python Application) acts as the master orchestrator for all synchronization signals.

### Key Components

1.  **SyncController (Python):**
    - Runs a **Serial Reader Thread** for constant, low-latency communication with the Arduino.
    - Runs an **Open Ephys Polling Thread** (HTTP GET `/api/status`).
    - Runs a **UDP Listener Thread** (Port 5005) for Matlab behavior triggers.
    - Implements **Multi-Source "OR Logic"**: The first enabled trigger source to fire starts the recording; the last active source to stop ends it.

2.  **Arduino Firmware:**
    - Acts as a **Slave Pulse Generator**.
    - Reports physical "Barcode" and "Camera" button presses as Serial strings (e.g., `EVENT:BARCODE_BUTTON`).
    - Defaults all TTL lines to **LOW** on startup.
    - Responds to specific commands: `'R'` (Start Sync), `'X'` (Stop Sync), `'C'`/`'E'` (Free Run Start/Stop).

3.  **CameraApp GUI (Python/Tkinter):**
    - Provides real-time preview (15fps).
    - Mode-specific logic (View Only, Free Record, Synced Record).
    - "Armed" state indicator for triggered recordings.

### Data Flow

1.  **Trigger Event:** Matlab (UDP), Open Ephys (HTTP), or Arduino (Serial Button) sends a "START" signal.
2.  **Arming & Execution:** If GUI is "Armed," Python sends `'R'` to Arduino.
3.  **Sync Pulse:** Arduino starts Barcodes immediately and Camera Pulses at the next barcode gap.
4.  **Capture:** `camCapture` thread detects frames and pushes to `imageWriteQueue`.
5.  **Metadata:** Initial `.json` stub written to disk.

## 🔵 Existing Patterns

This design follows and extends the existing pattern found in `sync_controller.py` and `camera_capture.py`:
- **Threaded Capture:** Maintains the existing `camCapture` and `saveImage` thread separation for performance.
- **Config-Driven:** Extends the `config.json` pattern to include network settings (IPs, Ports) and codec-specific flags.
- **Divergence:** Introduces a **Serial Reader Thread** in `SyncController` to replace the previous blocking/polled approach, ensuring physical buttons don't "race" against software commands.

## 🟡 Implementation Phases

> **Implementation status note (2026-03-28):** Phases 4 and 5 are already implemented in the current codebase. Phase 1 is partially done. Phases 2 and 3 are the primary remaining work.

<!-- START_PHASE_1 -->
### Phase 1: Robust Arduino Firmware
**Goal:** Harden the Arduino code for reliable, low-latency sync.

**Components:**
- `barcode_sync_millis_button/barcode_sync_millis_button.ino`: Modify button handlers to report button presses via Serial `EVENT:` strings instead of directly toggling pins.

**Current state:** All serial commands (`R/X/C/E/A/S/B/D/?`) are implemented. All pins default to LOW on startup. The `?` status query and `printStatus()` are implemented. **Remaining gap:** `updateBarcodeButton()` and `updateCamButton()` still directly toggle `runBarcode`/`runCam` instead of emitting `EVENT:BARCODE_BUTTON` / `EVENT:CAM_BUTTON` serial strings — Python cannot intercept button presses.

**Dependencies:** None

**Done when:** Arduino serial output shows `EVENT:BARCODE_BUTTON` and `EVENT:CAM_BUTTON` strings on button press; buttons no longer directly toggle pins (Python is the decision-maker).
<!-- END_PHASE_1 -->

<!-- START_PHASE_2 -->
### Phase 2: Unified SyncController (I/O Layer)
**Goal:** Build the remaining background communication threads.

**Components:**
- `sync_controller.py`: Add `SerialReaderThread` (reads incoming EVENT messages from Arduino) and `UDPListenerThread` (Port 5005, receives Matlab triggers). The `OEPollThread` is already implemented.
- `config.json`: Add `matlab_udp_port` (default 5005) to the `hardware` section.

**Current state:** OE polling thread is done. Serial send commands are done. Serial *reading* (EVENT messages from Arduino) and UDP listener are missing. `config.json` has no `matlab_udp_port` key.

**Dependencies:** Phase 1

**Done when:** `SyncController` correctly fires callbacks for "START" signals from all three sources (Matlab UDP, OE HTTP, Arduino Serial button events).
<!-- END_PHASE_2 -->

<!-- START_PHASE_3 -->
### Phase 3: Core State Machine & Trigger Logic
**Goal:** Implement multi-source "OR Logic", route all trigger sources, and send OE STOP.

**Components:**
- `camera_capture.py`: Add `_active_sources` set to track which sources are currently active. Route `EVENT:CAM_BUTTON` from Arduino and Matlab UDP signals to `_begin_recording` / `_end_recording`. Add OE HTTP STOP command after video finishes (AC2.1).

**Current state:** Single-source OE-triggered recording is implemented (`_oe_record_started` / `_oe_record_stopped` callbacks work). OR Logic (multi-source set), Matlab UDP routing, Arduino button routing, and OE STOP command are all missing.

**Dependencies:** Phase 2

**Done when:** Any enabled trigger source starts recording; the last source to stop ends it; Python sends HTTP STOP to Open Ephys after the video writer closes.
<!-- END_PHASE_3 -->

<!-- START_PHASE_4 -->
### Phase 4: Metadata & Fail-Safe Logging
**Goal:** Ensure every recording is documented, even on crash.

**Status: COMPLETE** — `make_metadata_stub`, `finalise_metadata`, and `write_metadata` are implemented in `camera_capture.py` and called correctly in `_begin_recording` (stub written immediately) and `_finish_worker` (finalised on clean stop). No further work needed.

**Done when:** (already done) Every recording start creates a `.json` file; every clean stop updates it to "complete."
<!-- END_PHASE_4 -->

<!-- START_PHASE_5 -->
### Phase 5: GUI Refinement & Mode Selection
**Goal:** Finalize the user interface for rig-wide operation.

**Status: COMPLETE** — Mode Selector (Triggered / Free Record / View Only radio buttons), Armed status label (`● ARMED — waiting for Open Ephys...`), and dynamic FPS combobox (enabled only in Free Record mode) are all implemented. "View Only" shows a messagebox and returns without writing to disk. The "Synced" mode ARMED state already waits for OE; completing Phase 3 will extend this to Matlab and button triggers.

**Done when:** (already done) GUI reflects hardware state correctly; "View Only" works without disk I/O; "Synced" mode waits correctly for external triggers.
<!-- END_PHASE_5 -->

## 🔵 Additional Considerations

**Error Handling:** If the Serial connection drops, the GUI should immediately transition to "ERROR" state and stop any active writer to prevent corrupted video files.

**Extensibility:** The UDP listener can easily be extended to handle custom "Experiment Tags" from Matlab to be included in the metadata.
