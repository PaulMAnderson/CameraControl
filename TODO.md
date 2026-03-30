# Unified Sync Controller Implementation

## Phase 1: Robust Arduino Firmware <!-- id: 1 -->
**Goal:** Harden the Arduino code for reliable, low-latency sync.
- [x] Modify `barcode_sync_millis_button/barcode_sync_millis_button.ino` to report dual button presses via Serial. <!-- id: 1.1 -->
- [x] Default all Arduino pins to LOW on startup. <!-- id: 1.2 -->
- [x] Implement `EVENT` strings on button press and `ACK` on commands. <!-- id: 1.3 -->
- [x] Verify pins are LOW on boot and Serial output is correct. <!-- id: 1.4 -->

## Phase 2: Unified SyncController (I/O Layer) <!-- id: 2 -->
**Goal:** Build the background communication threads.
- [x] Implement `SerialReaderThread` in `sync_controller.py`. <!-- id: 2.1 -->
- [x] Implement `UDPListenerThread` (Port 5005) for Matlab triggers. <!-- id: 2.2 -->
- [x] Implement `OEPollThread` for Open Ephys HTTP status updates. <!-- id: 2.3 -->
- [x] Update `config.json` with network and port configurations. <!-- id: 2.4 -->
- [x] Verify `SyncController` logs "START" signals from all three sources. <!-- id: 2.5 -->

## Phase 3: Core State Machine & Trigger Logic <!-- id: 3 -->
**Goal:** Implement the "OR Logic" and hardware arming.
- [x] Refactor `camera_capture.py` `_begin_recording` for multiple trigger sources. <!-- id: 3.1 -->
- [x] Refactor `camera_capture.py` `_stop_recording` for state-aware "OR Logic" stop. <!-- id: 3.2 -->
- [x] Implement hardware arming mechanism to send `'R'` to Arduino. <!-- id: 3.3 -->
- [x] Verify first enabled trigger source starts camera writer and sends `'R'`. <!-- id: 3.4 -->

## Phase 4: Metadata & Fail-Safe Logging <!-- id: 4 -->
**Goal:** Ensure every recording is documented, even on crash.
- [x] Implement `.json` stub writing on recording start in `camera_capture.py`. <!-- id: 4.1 -->
- [x] Implement metadata update logic on clean recording stop. <!-- id: 4.2 -->
- [x] Verify `.json` file creation and "complete" status updates. <!-- id: 4.3 -->

## Phase 5: GUI Refinement & Mode Selection <!-- id: 5 -->
**Goal:** Finalize the user interface for rig-wide operation.
- [x] Implement Mode Selector in GUI (View Only, Free Record, Synced Record). <!-- id: 5.1 -->
- [x] Implement "Armed" status indicator in GUI. <!-- id: 5.2 -->
- [x] Implement dynamic FPS controls in GUI. <!-- id: 5.3 -->
- [x] Verify "View Only" works without disk I/O. <!-- id: 5.4 -->
- [x] Verify "Synced" mode waits for external triggers. <!-- id: 5.5 -->
