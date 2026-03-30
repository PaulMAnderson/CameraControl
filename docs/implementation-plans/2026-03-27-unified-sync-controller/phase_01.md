---
phase: 1
title: Robust Arduino Firmware — Button Event Reporting
context-budget: small
files-required:
  - docs/design-plans/2026-03-27-unified-sync-controller.md
  - barcode_sync_millis_button/barcode_sync_millis_button.ino
depends-on: []
---

# Unified Sync Controller Implementation Plan

**Goal:** Decouple Arduino physical button presses from direct pin control so Python becomes the sole decision-maker for all recording state.

**Architecture:** The Arduino acts as a "Slave Pulse Generator." It reports hardware events upward to Python via structured serial strings (`EVENT:*`) and only acts on explicit commands sent down from Python (`R`, `X`, `C`, `E`, etc.). Physical buttons must never bypass this contract.

**Tech Stack:** Arduino C++ (no external libraries beyond Arduino core)

**Scope:** 3 phases of remaining work from original 5-phase design (Phases 4 & 5 already implemented)

**Codebase verified:** 2026-03-28

---

## Acceptance Criteria Coverage

This phase implements:

### unified-sync-controller.AC3: Bidirectional Arduino Sync
- **unified-sync-controller.AC3.1 Success:** External "Barcode Button" and "Camera Button" trigger separate `EVENT:BARCODE_BUTTON` and `EVENT:CAM_BUTTON` messages over Serial.
- **unified-sync-controller.AC3.2 Success:** Pressing the Camera Button starts a synced recording if the GUI is "Armed." *(Routing logic implemented in Phase 3 — this phase establishes the serial event that Phase 3 handles.)*
- **unified-sync-controller.AC3.3 Success:** Pressing the Barcode Button toggles the barcode generator on/off independently of video capture. *(Routing logic implemented in Phase 3.)*

---

<!-- START_SUBCOMPONENT_A (tasks 1-2) -->

<!-- START_TASK_1 -->
### Task 1: Refactor `updateBarcodeButton` to emit EVENT string

**Verifies:** unified-sync-controller.AC3.1

**Files:**
- Modify: `barcode_sync_millis_button/barcode_sync_millis_button.ino:215-227`

**Implementation:**

Replace the body of `updateBarcodeButton` so it emits a structured event string and does **not** touch `runBarcode`. Python receives the event and decides what to do.

Current code (lines 215–227):
```cpp
void updateBarcodeButton() {
  if ( ((currentMillis - previousBarcodeButton)     >= barcodeButtonDebounce) &&
       ((currentMillis - previousBarcodeActivation) >= barcodeButtonReactivate) ) {
    barcodeButtonState = digitalRead(barcodeButtonPin);
    if (barcodeButtonState == LOW) {
      Serial.println("Barcode Button pressed.");
      runBarcode = !runBarcode;
      Serial.println(runBarcode ? "Starting Barcode TTL Pulses..." : "Stopping Barcode TTL Pulses...");
      previousBarcodeActivation = currentMillis;
    }
    previousBarcodeButton = currentMillis;
  }
}
```

Replace with:
```cpp
void updateBarcodeButton() {
  if ( ((currentMillis - previousBarcodeButton)     >= barcodeButtonDebounce) &&
       ((currentMillis - previousBarcodeActivation) >= barcodeButtonReactivate) ) {
    barcodeButtonState = digitalRead(barcodeButtonPin);
    if (barcodeButtonState == LOW) {
      Serial.println("EVENT:BARCODE_BUTTON");
      previousBarcodeActivation = currentMillis;
    }
    previousBarcodeButton = currentMillis;
  }
}
```

**Verification:**
Upload to Arduino, open Serial Monitor at 9600 baud, press the barcode button. Expected output:
```
EVENT:BARCODE_BUTTON
```
The barcode pulses must NOT start or stop from the button press alone — only from a subsequent `B`/`D`/`R`/`X` command.

**Commit:** `feat(arduino): barcode button emits EVENT string instead of toggling pin directly`

<!-- END_TASK_1 -->

<!-- START_TASK_2 -->
### Task 2: Refactor `updateCamButton` to emit EVENT string

**Verifies:** unified-sync-controller.AC3.1

**Files:**
- Modify: `barcode_sync_millis_button/barcode_sync_millis_button.ino:229-242`

**Implementation:**

Replace the body of `updateCamButton` so it emits a structured event string and does **not** touch `runCam` or `camStartPending`.

Current code (lines 229–242):
```cpp
void updateCamButton() {
  if ( ((currentMillis - previousCamButton)       >= camButtonDebounce) &&
       ((currentMillis - previousCamActivation)   >= camButtonReactivate) ) {
    camButtonState = digitalRead(camButtonPin);
    if (camButtonState == LOW) {
      Serial.println("Cam Button pressed.");
      runCam          = !runCam;
      camStartPending = false; // manual button overrides any pending state
      Serial.println(runCam ? "Starting Camera TTL Pulses..." : "Stopping Camera TTL Pulses...");
      previousCamActivation = currentMillis;
    }
    previousCamButton = currentMillis;
  }
}
```

Replace with:
```cpp
void updateCamButton() {
  if ( ((currentMillis - previousCamButton)       >= camButtonDebounce) &&
       ((currentMillis - previousCamActivation)   >= camButtonReactivate) ) {
    camButtonState = digitalRead(camButtonPin);
    if (camButtonState == LOW) {
      Serial.println("EVENT:CAM_BUTTON");
      previousCamActivation = currentMillis;
    }
    previousCamButton = currentMillis;
  }
}
```

**Verification:**
Upload to Arduino, open Serial Monitor at 9600 baud, press the camera button. Expected output:
```
EVENT:CAM_BUTTON
```
Camera TTL pulses must NOT start or stop from the button press alone — only from a subsequent `C`/`E`/`R`/`X` command.

**Commit:** `feat(arduino): cam button emits EVENT string instead of toggling pin directly`

<!-- END_TASK_2 -->

<!-- END_SUBCOMPONENT_A -->

---

## End-of-Phase Verification

After both tasks are complete and firmware is uploaded:

1. Open Serial Monitor at 9600 baud.
2. Press barcode button → see `EVENT:BARCODE_BUTTON`, no pin activity.
3. Press cam button → see `EVENT:CAM_BUTTON`, no pin activity.
4. Send `R` command → barcodes start, camera TTLs queue at next barcode boundary.
5. Send `X` command → camera TTLs stop, barcodes finish cleanly.
6. Send `?` → STATUS line shows correct state.
7. Power-cycle → all pins are LOW at boot.

All of these must pass before proceeding to Phase 2.
