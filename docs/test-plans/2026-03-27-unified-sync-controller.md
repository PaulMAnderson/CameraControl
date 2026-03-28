# Human Test Plan: Unified Sync Controller

**Implementation:** `docs/implementation-plans/2026-03-27-unified-sync-controller/`
**Date:** 2026-03-28
**Test type:** Manual hardware verification (no automated test suite)

---

## Prerequisites

- FLIR camera connected via USB and recognized by PySpin/Spinnaker SDK
- Arduino Uno/Mega flashed with `barcode_sync_millis_button/barcode_sync_millis_button.ino` (Phase 1 firmware), connected via USB (port matching `config.json` `hardware.arduino_port`, default `COM3`)
- Physical barcode button wired to Arduino pin 7 (INPUT_PULLUP, active LOW)
- Physical camera button wired to Arduino pin 6 (INPUT_PULLUP, active LOW)
- Oscilloscope or LED indicators on Arduino pins 8 (TTL), 9 (barcode), 10 (camera)
- Open Ephys GUI running on `localhost:37497` (or host/port matching `config.json`)
- `config.json` configured with correct paths and hardware settings for the test rig
- Python environment with `pyserial`, `PySpin`, and other dependencies installed
- Output directory (`D:\Video` or as configured) exists and is writable

---

## Phase 1: Arduino Firmware Verification (AC3.1)

| Step | Action | Expected |
|------|--------|----------|
| 1.1 | Upload `barcode_sync_millis_button.ino` to Arduino. Open Arduino Serial Monitor at 9600 baud. | Serial Monitor shows `Serial Initialised...` followed by `STATUS barcode=0 cam=0 camPending=0 barcodeStopPending=0`. |
| 1.2 | Press the barcode button (pin 7). | Serial Monitor prints `EVENT:BARCODE_BUTTON`. No barcode TTL pulses on pin 9. |
| 1.3 | Press the camera button (pin 6). | Serial Monitor prints `EVENT:CAM_BUTTON`. No camera TTL pulses on pin 10. |
| 1.4 | Type `R` in Serial Monitor and press Enter. | Barcode pulses begin on pin 9. After one barcode cycle (~10s), camera TTL pulses begin on pin 10. |
| 1.5 | Type `X` in Serial Monitor. | Camera pin 10 goes LOW immediately. Barcodes finish current cycle then pin 9 goes LOW. |
| 1.6 | Type `B` then `D`. | `B` starts barcode pulses on pin 9. `D` stops them. Camera pin 10 unaffected. |
| 1.7 | Type `C` then `E`. | `C` starts camera TTL pulses on pin 10 at ~400 Hz. `E` stops them. Barcode pin 9 unaffected. |
| 1.8 | Type `S` to stop all. | All pins go LOW. |
| 1.9 | Power-cycle the Arduino. | All output pins (8, 9, 10) are LOW at boot. |
| 1.10 | Type `?`. | Serial prints `STATUS barcode=0 cam=0 camPending=0 barcodeStopPending=0`. |

---

## Phase 2: SyncController Thread Startup (AC1.1)

| Step | Action | Expected |
|------|--------|----------|
| 2.1 | Ensure Arduino connected, Open Ephys running. Launch `camera_capture.py`. | GUI appears within 5-10 seconds. No crash. |
| 2.2 | Open `camera_capture.log`. Search for thread start messages. | Log contains `SerialReaderThread started`, OE polling and UDP listener start messages. No `Exception` or `Traceback` entries. |
| 2.3 | Leave the application idle for 30 seconds. | No thread-crash messages, no exceptions, no timeout errors in log. |
| 2.4 | Close the application via the window close button. | No `thread.join` timeout errors. Clean shutdown messages present. |

---

## Phase 3: UDP Trigger — Matlab START/STOP (AC1.2)

| Step | Action | Expected |
|------|--------|----------|
| 3.1 | Launch `camera_capture.py`. Select **Triggered** mode. Enter animal name `test_udp`. Press **Record**. | GUI shows `ARMED`. |
| 3.2 | Run: `python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'START', ('127.0.0.1', 5005))"` | GUI transitions from `ARMED` to `RECORDING`. Frame counter starts. |
| 3.3 | Wait 5 seconds. | Frame counter increments steadily. No errors in log. |
| 3.4 | Run: `python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'STOP', ('127.0.0.1', 5005))"` | Recording stops. GUI returns to `IDLE`. Video file and `.json` sidecar created. |

---

## Phase 4: Open Ephys RECORD Trigger (AC1.3)

| Step | Action | Expected |
|------|--------|----------|
| 4.1 | Launch `camera_capture.py`. Open Ephys in ACQUIRE. Select **Triggered** mode, enter `test_oe`, press **Record**. | GUI shows `ARMED`. |
| 4.2 | In Open Ephys, click the Record button. | Within 1 second, camera GUI transitions from `ARMED` to `RECORDING`. Frame counter increments. |
| 4.3 | Press **Stop** in the camera GUI. | Recording stops, GUI returns to `IDLE`. |

---

## Phase 5: Arduino Disconnect Handling (AC1.4)

| Step | Action | Expected |
|------|--------|----------|
| 5.1 | Launch `camera_capture.py` with Arduino connected. | GUI healthy, Record button enabled. |
| 5.2 | Physically unplug the Arduino USB cable. | Within 5 seconds, GUI shows prominent error. **Record** button becomes disabled. |
| 5.3 | Attempt to click the Record button. | Nothing happens. |
| 5.4 | Check `camera_capture.log`. | SerialException caught, `_serial_ok` set to False. |
| 5.5 | Reconnect Arduino and restart application. | Application starts normally. |

---

## Phase 6: Video PC Stops Open Ephys (AC2.1)

| Step | Action | Expected |
|------|--------|----------|
| 6.1 | Launch `camera_capture.py`. Open Ephys in ACQUIRE. Select **Triggered** mode, enter `test_oe_stop`, press **Record** (ARMED). | GUI shows `ARMED`. |
| 6.2 | Start recording in Open Ephys. | Camera GUI transitions to `RECORDING`. |
| 6.3 | Press **Stop** in the camera GUI. Wait for write to finish. | GUI returns to `IDLE`. File label shows saved filename. |
| 6.4 | Check Open Ephys. | OE has automatically returned to IDLE or ACQUIRE (no longer RECORD). |
| 6.5 | Check `camera_capture.log`. | No exception from `cmd_stop_oe_recording`. |

---

## Phase 7: Multi-Source OR Logic (AC2.2)

| Step | Action | Expected |
|------|--------|----------|
| 7.1 | Launch `camera_capture.py`. Open Ephys running. Select **Triggered** mode, enter `test_or_logic`, press **Record** (ARMED). | GUI shows `ARMED`. |
| 7.2 | Send Matlab UDP START from terminal. | Recording starts. Active sources = `{matlab}`. |
| 7.3 | Start recording in Open Ephys. | Recording continues. Active sources = `{matlab, oe}`. |
| 7.4 | Stop recording in Open Ephys. | Recording **continues** in camera GUI. Frame counter keeps incrementing. |
| 7.5 | Send Matlab UDP STOP. | Recording stops. GUI returns to `IDLE`. |

---

## Phase 8: GUI Stop Override (AC2.3)

| Step | Action | Expected |
|------|--------|----------|
| 8.1 | Launch `camera_capture.py`. Select **Triggered** mode, enter `test_override`, press **Record** (ARMED). | GUI shows `ARMED`. |
| 8.2 | Send Matlab UDP START. | Recording starts. |
| 8.3 | Press the physical camera button. | Source added to active set. |
| 8.4 | Press the **Stop** button in the GUI. | Recording stops **immediately**. GUI returns to `IDLE`. |
| 8.5 | Verify output files. | Video file and `.json` sidecar created and valid. |
| 8.6 | Send a stale UDP STOP from terminal. | No error occurs. |

---

## Phase 9: Camera Button Synced Recording (AC3.2)

| Step | Action | Expected |
|------|--------|----------|
| 9.1 | Launch `camera_capture.py`. Select **Triggered** mode, enter `test_cam_btn`, press **Record** (ARMED). | GUI shows `ARMED`. |
| 9.2 | Press the physical **camera button**. | GUI transitions from `ARMED` to `RECORDING`. Frame counter increments. |
| 9.3 | Wait 5 seconds. Press camera button again. | Recording stops. GUI returns to `IDLE`. Video and sidecar created. |
| 9.4 | With GUI in `IDLE` (not ARMED), press camera button. | Nothing happens. No recording starts. |

---

## Phase 10: Barcode Button Independent Toggle (AC3.3)

| Step | Action | Expected |
|------|--------|----------|
| 10.1 | Launch `camera_capture.py`. GUI in `IDLE`. | No TTL activity. |
| 10.2 | Press the physical **barcode button**. | Barcode TTL pulses start on pin 9. Recording does NOT start. |
| 10.3 | Press the barcode button again. | Barcode TTL pulses stop. |
| 10.4 | Start a recording (Free Record mode, any FPS). | Recording begins. |
| 10.5 | Press barcode button during recording. | Barcodes toggle on independently. Camera TTLs unaffected. |
| 10.6 | Press barcode button again. | Barcodes toggle off. Recording continues. |
| 10.7 | Stop the recording. | GUI returns to `IDLE`. |

---

## Phase 11: Regression — Fail-Safe Metadata (AC4.1, AC4.2, AC4.3)

| Step | Action | Expected |
|------|--------|----------|
| 11.1 | Launch `camera_capture.py`. Select **Free Record**, 30fps. Enter `test_metadata`. Watch output directory. | Ready to record. |
| 11.2 | Press **Record**. Within 1 second, check output directory. | New `.json` sidecar file appears. |
| 11.3 | Open the `.json` file during recording. | Contains `"status": "recording"`, animal name, date, metadata fields. |
| 11.4 | Let recording run ≥10 seconds. Press **Stop**. Note frame count in GUI. | GUI returns to `IDLE`, shows saved file summary. |
| 11.5 | Open the `.json` sidecar. | Contains `"status": "complete"`, positive `frames_saved` matching GUI, `frames_dropped` present. |
| 11.6 | Compare `frames_dropped` in JSON vs GUI summary vs `camera_capture.log`. | All three values match exactly. |
| 11.7 | Verify video file. | File exists, non-zero size, plays in media player. |

---

## Phase 12: Regression — Mode-Specific Behavior (AC5.1, AC5.2)

| Step | Action | Expected |
|------|--------|----------|
| 12.1 | Launch `camera_capture.py`. Select **View Only** mode. | Camera preview displayed, updating at ~15fps. |
| 12.2 | Monitor Arduino TTL pins 8, 9, 10 with oscilloscope/LEDs. | No TTL pulses of any kind. All pins LOW. |
| 12.3 | Attempt to press **Record**. | Application shows warning or prevents recording. No file written. |
| 12.4 | Remain in View Only for 30 seconds. | Preview continues. No TTL activity. No files created. |
| 12.5 | Switch to **Free Record** mode, select 30fps. Enter `test_free`, press **Record**. | GUI shows `RECORDING`. Frame counter increments. |
| 12.6 | Monitor camera TTL output (pin 10) with oscilloscope. | Pulses at ~33ms period (30 Hz), within 5% tolerance. |
| 12.7 | Press **Stop**. | Camera TTL pulses stop. GUI returns to `IDLE`. Video file and sidecar created. |
| 12.8 | Open the `.json` sidecar. | Framerate field matches the selected value. |

---

## End-to-End: Full Triggered Recording Workflow

*Validates AC1.2, AC1.3, AC2.1, AC2.2, AC4.1, AC4.2, AC4.3 in a single session.*

| Step | Action | Expected |
|------|--------|----------|
| E1 | Launch `camera_capture.py` with all hardware connected. Open Ephys in ACQUIRE. | Clean startup, no errors in log. |
| E2 | Select **Triggered** mode. Enter `e2e_test`. Press **Record**. | GUI shows `ARMED`. |
| E3 | Send Matlab UDP START. | GUI transitions to `RECORDING`. `.json` sidecar appears with `"status": "recording"`. |
| E4 | Start recording in Open Ephys. | Recording continues. Active sources = `{matlab, oe}`. |
| E5 | Stop recording in Open Ephys. | Recording **continues** (Matlab still active). |
| E6 | Send Matlab UDP STOP. | Recording stops. GUI returns to `IDLE`. |
| E7 | Check Open Ephys. | OE has returned to IDLE automatically. |
| E8 | Open the `.json` sidecar. | `"status": "complete"`, positive `frames_saved`, `frames_dropped` matching GUI/log. |
| E9 | Play the video file. | Plays correctly, frame count consistent with recording duration and framerate. |

---

## End-to-End: Arduino Button-Driven Session

*Validates AC3.1, AC3.2, AC3.3, AC4.1, AC4.2 in a single session.*

| Step | Action | Expected |
|------|--------|----------|
| F1 | Launch `camera_capture.py`. Select **Triggered** mode. Enter `btn_e2e`. Press **Record**. | GUI shows `ARMED`. |
| F2 | Press barcode button. | Barcodes start (pin 9 active). Recording does NOT start. |
| F3 | Press camera button. | Recording starts (ARMED → RECORDING). Camera TTLs begin on pin 10 after barcode boundary. |
| F4 | Verify both running simultaneously. | Oscilloscope/LEDs show both pin 9 and pin 10 active. |
| F5 | Press barcode button. | Barcodes stop. Recording continues (camera TTLs unaffected). |
| F6 | Press camera button. | Recording stops. GUI returns to `IDLE`. |
| F7 | Open `.json` sidecar. | `"status": "complete"`, valid frame counts. |

---

## Traceability Matrix

| Acceptance Criterion | Manual Phase |
|----------------------|-------------|
| AC1.1 Background threads start | Phase 2, steps 2.1-2.4 |
| AC1.2 Matlab UDP START triggers recording | Phase 3, steps 3.1-3.4 |
| AC1.3 Open Ephys RECORD triggers recording | Phase 4, steps 4.1-4.3 |
| AC1.4 Arduino disconnect error | Phase 5, steps 5.1-5.5 |
| AC2.1 Video PC sends STOP to OE | Phase 6, steps 6.1-6.5 |
| AC2.2 Last-source-stops logic | Phase 7, steps 7.1-7.5 |
| AC2.3 GUI Stop overrides all | Phase 8, steps 8.1-8.6 |
| AC3.1 Buttons emit EVENT strings | Phase 1, steps 1.2-1.3 |
| AC3.2 Camera button starts recording | Phase 9, steps 9.1-9.4 |
| AC3.3 Barcode button toggles independently | Phase 10, steps 10.1-10.7 |
| AC4.1 JSON sidecar within 500ms | Phase 11, steps 11.1-11.3 |
| AC4.2 Clean shutdown updates metadata | Phase 11, steps 11.4-11.5 |
| AC4.3 Dropped frame count matches | Phase 11, step 11.6 |
| AC5.1 View Only no TTL/barcode | Phase 12, steps 12.1-12.4 |
| AC5.2 Free Record software-rate pulses | Phase 12, steps 12.5-12.8 |
