---
phase: 2
title: Unified SyncController — Serial Reader & UDP Listener Threads
context-budget: medium
files-required:
  - docs/design-plans/2026-03-27-unified-sync-controller.md
  - sync_controller.py
  - config.json
depends-on: [phase_01]
---

# Unified Sync Controller Implementation Plan

**Goal:** Add the two missing I/O threads to `SyncController`: a background `SerialReaderThread` that receives incoming `EVENT:*` strings from the Arduino, and a `UDPListenerThread` that receives start/stop triggers from Matlab.

**Architecture:** Both threads are daemon threads owned by `SyncController`. They fire callbacks onto the tkinter main thread using the existing `_fire()` helper, keeping the same thread-safety contract as the OE polling thread. The serial reader starts only after `connect_arduino()` completes its handshake, preventing a race on the startup `?` query.

**Tech Stack:** Python standard library — `threading`, `socket`, `pyserial` (already installed)

**Scope:** Phase 2 of 3 remaining implementation phases

**Codebase verified:** 2026-03-28

---

## Acceptance Criteria Coverage

This phase establishes the transport layer that Phase 3 routes to recording logic.

### unified-sync-controller.AC1: Unified Sync Master
- **unified-sync-controller.AC1.1 Success:** `SyncController` starts background threads for Serial, UDP, and HTTP polling without crashing.
- **unified-sync-controller.AC1.2 Success:** Matlab UDP "START" packet (Port 5005) triggers the internal `start_recording` sequence. *(Transport established here; routing wired in Phase 3.)*

### unified-sync-controller.AC3: Bidirectional Arduino Sync
- **unified-sync-controller.AC3.1 Success:** External "Barcode Button" and "Camera Button" trigger separate `EVENT:BARCODE_BUTTON` and `EVENT:CAM_BUTTON` messages. *(Messages received and logged here; routing wired in Phase 3.)*

---

<!-- START_SUBCOMPONENT_A (tasks 1-2) -->

<!-- START_TASK_1 -->
### Task 1: Add `matlab_udp_port` to `config.json`

**Verifies:** configuration prerequisite for unified-sync-controller.AC1.2

**Files:**
- Modify: `config.json:14-19` (hardware section)

**Implementation:**

Add `"matlab_udp_port": 5005` as the last key in the `hardware` section.

Current `hardware` section (lines 14–19):
```json
"hardware": {
    "arduino_port":      "COM3",
    "open_ephys_host":   "localhost",
    "open_ephys_port":   37497,
    "open_ephys_poll_interval_ms": 500
},
```

Replace with:
```json
"hardware": {
    "arduino_port":      "COM3",
    "open_ephys_host":   "localhost",
    "open_ephys_port":   37497,
    "open_ephys_poll_interval_ms": 500,
    "matlab_udp_port":   5005
},
```

**Verification:**
```python
import json
cfg = json.load(open('config.json'))
assert cfg['hardware']['matlab_udp_port'] == 5005
print("OK")
```

**Commit:** `config: add matlab_udp_port to hardware section`

<!-- END_TASK_1 -->

<!-- START_TASK_2 -->
### Task 2: Add `SerialReaderThread` to `SyncController`

**Verifies:** unified-sync-controller.AC3.1 (transport), unified-sync-controller.AC1.1

**Files:**
- Modify: `sync_controller.py`

**Implementation:**

Add the following to `SyncController`. Insert instance variable initialisations in `__init__` and add the three new methods after `disconnect_arduino`.

**In `__init__` — add these lines after `self._serial_ok = False` (line 37):**
```python
self._serial_thread = None
self._serial_stop   = threading.Event()
self._on_arduino_event = None
```

**New public method — add after `disconnect_arduino` (after line 77):**
```python
def start_serial_reader(self, on_arduino_event=None):
    """Start background thread reading EVENT: lines from Arduino.
    Call this after connect_arduino() succeeds.
    on_arduino_event(event: str) — called with the event name, e.g. 'BARCODE_BUTTON'.

    WARNING: Once this thread is running, do NOT call query_status() — both
    methods read from the same serial port and will race. The startup handshake
    in connect_arduino() sends '?' and reads the STATUS response *before* this
    thread is started, so the normal call sequence is safe.
    """
    self._on_arduino_event = on_arduino_event
    self._serial_stop.clear()
    self._serial_thread = threading.Thread(
        target=self._serial_reader_loop, daemon=True, name="SerialReaderThread"
    )
    self._serial_thread.start()

def stop_serial_reader(self):
    self._serial_stop.set()
    if self._serial_thread:
        self._serial_thread.join(timeout=3.0)

def _serial_reader_loop(self):
    while not self._serial_stop.is_set():
        if not (self._serial_ok and self._serial and self._serial.is_open):
            self._serial_stop.wait(0.1)
            continue
        try:
            line = self._serial.readline().decode('utf-8', errors='ignore').strip()
            if line.startswith('EVENT:'):
                event = line[6:]   # e.g. 'BARCODE_BUTTON'
                self._fire(self._on_arduino_event, event)
        except serial.SerialException:
            self._serial_ok = False
            self._fire_status()
            break
```

**Update `disconnect_arduino` to also stop the reader thread (line 68–77):**
```python
def disconnect_arduino(self):
    """Send stop-all before closing so pins go low."""
    self.stop_serial_reader()
    if self._serial and self._serial.is_open:
        try:
            self._serial.write(b'S')
            time.sleep(0.1)
            self._serial.close()
        except Exception:
            pass
    self._serial_ok = False
```

**Verification:**
1. Flash Phase 1 Arduino firmware (button events now emit `EVENT:*`).
2. Run `camera_capture.py`, connect Arduino successfully.
3. In `camera_capture.py` `__init__`, temporarily add after `_connect_arduino()`:
   ```python
   self.sync.start_serial_reader(on_arduino_event=lambda e: print(f"ARDUINO EVENT: {e}"))
   ```
4. Press the barcode button → `camera_capture.log` shows `ARDUINO EVENT: BARCODE_BUTTON`.
5. Press the cam button → `camera_capture.log` shows `ARDUINO EVENT: CAM_BUTTON`.
   *(Note: `print()` output lands in `camera_capture.log` because `_setup_logging()` at startup redirects `sys.stdout` to the log file.)*
6. Remove the temporary debug line.

**Commit:** `feat(sync): add SerialReaderThread to SyncController for incoming Arduino events`

<!-- END_TASK_2 -->

<!-- END_SUBCOMPONENT_A -->

---

<!-- START_SUBCOMPONENT_B (tasks 3-4) -->

<!-- START_TASK_3 -->
### Task 3: Add `UDPListenerThread` to `SyncController`

**Verifies:** unified-sync-controller.AC1.1, unified-sync-controller.AC1.2 (transport)

**Files:**
- Modify: `sync_controller.py`

**Implementation:**

Add to `SyncController`. Insert instance variable initialisations in `__init__` and add the three new methods after `stop_serial_reader`.

**Add `import socket` to the top of `sync_controller.py`** alongside the existing imports (after `import serial.tools.list_ports`, line 9):
```python
import socket
```

**In `__init__` — add these lines after the serial thread variables:**
```python
self._udp_thread      = None
self._udp_stop        = threading.Event()
self._udp_sock        = None   # kept as instance var so stop_udp_listener can close it
self._on_matlab_start = None
self._on_matlab_stop  = None
```

**New methods — add after `_serial_reader_loop`:**
```python
def start_udp_listener(self, on_matlab_start=None, on_matlab_stop=None):
    """Start background thread listening for Matlab UDP triggers.
    Binds to 0.0.0.0:{matlab_udp_port} (from config.json hardware section).
    on_matlab_start() — called when 'START' datagram received.
    on_matlab_stop()  — called when 'STOP' datagram received.
    """
    self._on_matlab_start = on_matlab_start
    self._on_matlab_stop  = on_matlab_stop
    self._udp_stop.clear()
    self._udp_thread = threading.Thread(
        target=self._udp_listener_loop, daemon=True, name="UDPListenerThread"
    )
    self._udp_thread.start()

def stop_udp_listener(self):
    self._udp_stop.set()
    if self._udp_sock:
        try:
            self._udp_sock.close()   # unblocks recvfrom immediately
        except Exception:
            pass
    if self._udp_thread:
        self._udp_thread.join(timeout=3.0)

def _udp_listener_loop(self):
    port = self._cfg['hardware']['matlab_udp_port']
    self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self._udp_sock.settimeout(1.0)
    try:
        self._udp_sock.bind(('', port))
        while not self._udp_stop.is_set():
            try:
                data, _ = self._udp_sock.recvfrom(1024)
                msg = data.decode('utf-8', errors='ignore').strip()
                if msg == 'START':
                    self._fire(self._on_matlab_start)
                elif msg == 'STOP':
                    self._fire(self._on_matlab_stop)
            except (socket.timeout, OSError):
                continue
    finally:
        self._udp_sock = None
```

**Verification:**
1. Run `camera_capture.py` (or a temporary standalone test script).
2. Temporarily add in `CameraApp.__init__` after `_connect_arduino()`:
   ```python
   self.sync.start_udp_listener(
       on_matlab_start=lambda: print("MATLAB START"),
       on_matlab_stop=lambda: print("MATLAB STOP"),
   )
   ```
3. From terminal (same machine), send a test UDP packet:
   ```bash
   python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'START', ('127.0.0.1', 5005))"
   ```
4. Check `camera_capture.log` — see `MATLAB START`.
5. Repeat with `b'STOP'` → see `MATLAB STOP`.
6. Remove the temporary debug callbacks.

**Commit:** `feat(sync): add UDPListenerThread to SyncController for Matlab triggers`

<!-- END_TASK_3 -->

<!-- START_TASK_4 -->
### Task 4: Add `cmd_stop_oe_recording` to `SyncController`

**Verifies:** prerequisite for unified-sync-controller.AC2.1 (wired in Phase 3)

**Files:**
- Modify: `sync_controller.py`

**Implementation:**

Add a method that sends `PUT /api/status {"mode": "IDLE"}` to Open Ephys. Use the existing `urllib.request` import (already at line 11). Add after `stop_polling`.

```python
def cmd_stop_oe_recording(self):
    """Send HTTP PUT to Open Ephys to stop recording (mode=IDLE).
    Best-effort: silently swallows errors so caller is not blocked.
    Call this after the video writer has finished and closed.
    """
    if not _HAS_URLLIB:
        return
    host = self._cfg['hardware']['open_ephys_host']
    port = self._cfg['hardware']['open_ephys_port']
    url  = f"http://{host}:{port}/api/status"
    data = _json.dumps({"mode": "IDLE"}).encode('utf-8')
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method='PUT'
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0):
            pass
    except Exception:
        pass
```

**Verification:**
With Open Ephys running and recording, call from Python console:
```python
import json
cfg = json.load(open('config.json'))
from sync_controller import SyncController
sc = SyncController(cfg)
sc.cmd_stop_oe_recording()
# Open Ephys should stop recording and return to IDLE state
```

**Commit:** `feat(sync): add cmd_stop_oe_recording() for HTTP PUT to Open Ephys`

<!-- END_TASK_4 -->

<!-- END_SUBCOMPONENT_B -->

---

## End-of-Phase Verification

After all four tasks are complete:

1. Launch `camera_capture.py` with Arduino connected.
2. Check log confirms all three threads start: `OEPollThread`, `SerialReaderThread`, `UDPListenerThread`.
3. Press Arduino barcode button → log shows `ARDUINO EVENT: BARCODE_BUTTON`.
4. Press Arduino cam button → log shows `ARDUINO EVENT: CAM_BUTTON`.
5. Send UDP `START` from terminal → log shows Matlab start callback fired.
6. Send UDP `STOP` → log shows Matlab stop callback fired.
7. OE polling continues to show correct state changes.

All of these must pass before proceeding to Phase 3.
