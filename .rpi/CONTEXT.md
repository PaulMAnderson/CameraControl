# Session Context

_Last updated: 2026-03-28 - Implementation Plan Complete_

## Session Intent
The user is restructuring a high-speed camera capture system used in ephys recordings to ensure robust synchronization between video, electrophysiology (Open Ephys), and behavioral data (Matlab). The goal is to create an "idiot-proof" system where the Video PC acts as a central hub, orchestrating triggers and ensuring video capture perfectly brackets the ephys recording. A full implementation plan (3 active phases + test requirements) has been written, reviewed, and approved — ready for execution.

## Files Modified
- `barcode_sync_millis_button/barcode_sync_millis_button.ino`: Arduino firmware for TTL pulse generation and barcode sync.
- `camera_capture.py`: Main Python GUI application using PySpin and skvideo.
- `sync_controller.py`: Python class managing Serial, UDP, and HTTP communication.
- `config.json`: Per-rig configuration file for IPs, ports, and hardware settings.
- `launch_camera.bat`: Windows batch launcher using `pythonw.exe` for console-less operation.
- `docs/design-plans/2026-03-27-unified-sync-controller.md`: Updated with implementation status notes (Phases 4 & 5 marked COMPLETE; Phases 1-3 updated with current-state gap analysis).
- `TODO.md`: Design phase tracker.
- `.gitignore`: Added to exclude logs and pycache.
- `docs/implementation-plans/2026-03-27-unified-sync-controller/phase_01.md`: Created — Arduino button firmware changes (emit EVENT strings).
- `docs/implementation-plans/2026-03-27-unified-sync-controller/phase_02.md`: Created — SerialReaderThread, UDPListenerThread, cmd_stop_oe_recording in SyncController + config.json matlab_udp_port.
- `docs/implementation-plans/2026-03-27-unified-sync-controller/phase_03.md`: Created — OR Logic (_active_sources), all trigger routing, OE STOP after video close in CameraApp.
- `docs/implementation-plans/2026-03-27-unified-sync-controller/test-requirements.md`: Created — manual hardware verification procedures for all 15 ACs.

## Decisions Made
- **Centralized Hub Architecture:** The Video PC (Python) acts as the master, polling Open Ephys and listening for Matlab UDP triggers to orchestrate the Arduino.
- **Multi-Source "OR Logic":** Any enabled trigger (Matlab, OE, or Manual Button) can start a recording; the last active source to stop ends it.
- **Python-as-Master for Buttons:** Physical buttons on the Arduino report events to Python via Serial (`EVENT:BARCODE_BUTTON`, `EVENT:CAM_BUTTON`), allowing the GUI to enforce state-aware logic instead of the Arduino acting independently.
- **Fail-Safe Metadata:** Immediate generation of a `.json` "recording" stub on start to preserve metadata even during crashes. Already implemented in camera_capture.py.
- **Open Ephys Stop Command:** The Video PC explicitly sends a `PUT /api/status {"mode": "IDLE"}` to Open Ephys after the video writer closes — Python is master, OE is slave for stop.
- **OE stop callback is a no-op:** `_oe_record_stopped` intentionally does nothing; Python controls when recording ends.
- **Phases 4 & 5 already implemented:** Metadata stub/finalise functions and GUI (mode selector, armed state, FPS controls) exist in camera_capture.py and were excluded from the implementation plan.
- **UDP socket stored as instance var:** `self._udp_sock` kept on SyncController so `stop_udp_listener()` can close it immediately to unblock `recvfrom()` rather than waiting for timeout.
- **`query_status()` deprecated while SerialReaderThread runs:** Both read the same serial port; the startup handshake is done before the reader thread starts (safe call sequence preserved).
- **No automated tests:** Project has no test infrastructure; all verification is manual with live hardware. test-requirements.md documents manual procedures.
- **`import socket` at module level:** Moved from inside `_udp_listener_loop` to top of sync_controller.py for consistency with existing codebase pattern.

## Current State
Implementation planning is **complete and approved**. Three phase files + test-requirements.md written to `docs/implementation-plans/2026-03-27-unified-sync-controller/`. The code-reviewer approved all phases with zero remaining issues after 7 fixes were applied. Branch is `unified-sync-controller`. Context is being compressed before `/clear` for execution handoff.

**What exists vs. what needs implementing:**
- Phase 1 (Arduino): Commands done ✓, pins LOW on boot ✓. **REMAINING:** `updateBarcodeButton` and `updateCamButton` still directly toggle pins — need to emit `EVENT:BARCODE_BUTTON` / `EVENT:CAM_BUTTON` instead.
- Phase 2 (SyncController): OEPollThread done ✓, serial send done ✓. **REMAINING:** SerialReaderThread, UDPListenerThread, `cmd_stop_oe_recording()`, `cmd_start_barcodes()`, `cmd_stop_barcodes()`, `matlab_udp_port` in config.json.
- Phase 3 (CameraApp): OE single-source trigger done ✓, metadata done ✓, GUI done ✓. **REMAINING:** `_active_sources` OR Logic, Arduino event routing, Matlab UDP routing, OE STOP after video close, remove redundant `_oe_record_started()` call from `_oe_status_changed`.

## Next Steps
1. Copy the execute command below BEFORE running `/clear`.
2. Run `/clear` to reset context.
3. Run: `/rpi-plan-and-execute:execute-implementation-plan /Users/paul/Library/Mobile\ Documents/com~apple~CloudDocs/Development/Matlab/Plugins/CameraControl/docs/implementation-plans/2026-03-27-unified-sync-controller/ /Users/paul/Library/Mobile\ Documents/com~apple~CloudDocs/Development/Matlab/Plugins/CameraControl/`
4. Execute Phase 1: flash updated Arduino firmware (2 tasks).
5. Execute Phase 2: add SerialReaderThread + UDPListenerThread to sync_controller.py (4 tasks).
6. Execute Phase 3: wire OR Logic into camera_capture.py (5 tasks).
