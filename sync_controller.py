# sync_controller.py
# Manages Arduino serial communication and Open Ephys HTTP polling.
# Designed to be owned by the GUI - all callbacks are fired via tkinter's
# after() mechanism so they land safely on the main thread.

import threading
import time
import serial
import serial.tools.list_ports
import socket

try:
    import urllib.request
    import json as _json
    _HAS_URLLIB = True
except ImportError:
    _HAS_URLLIB = False


class SyncController:
    """
    Owns two background concerns:
      1. Arduino serial - sends command bytes, parses '?' status responses
      2. Open Ephys HTTP polling - watches /api/status, fires on_record_start
         and on_record_stop callbacks when state changes
    """

    # ------------------------------------------------------------------ init
    def __init__(self, config: dict, tk_root=None):
        """
        config   : the 'hardware' + 'camera' sections of config.json
        tk_root  : tkinter root window, used to schedule callbacks safely.
                   If None, callbacks are called directly (testing only).
        """
        self._cfg        = config
        self._tk         = tk_root
        self._serial     = None
        self._serial_ok  = False

        self._serial_thread = None
        self._serial_stop   = threading.Event()
        self._on_arduino_event = None

        self._udp_thread      = None
        self._udp_stop        = threading.Event()
        self._udp_sock        = None   # kept as instance var so stop_udp_listener can close it
        self._on_matlab_start = None
        self._on_matlab_stop  = None

        self._oe_thread  = None
        self._oe_stop    = threading.Event()
        self._oe_state   = None          # last known OE state string

        self._on_record_start = None
        self._on_record_stop  = None
        self._on_status_change = None    # general status update -> (arduino_ok, oe_state)

    # --------------------------------------------------------- Arduino serial
    def connect_arduino(self) -> tuple[bool, str, dict]:
        """
        Open serial connection to Arduino. Returns (success, message, state_dict).
        Sends '?' and parses the STATUS response so we know current state.
        """
        port = self._cfg['hardware']['arduino_port']
        state = {'barcode': 0, 'cam': 0}
        try:
            # On Due Native Port, baudrate is ignored but we'll stick to 9600.
            # dsrdtr=True is often required for the Due Native Port to communicate.
            self._serial = serial.Serial(port, 9600, timeout=1.0, dsrdtr=True)
            
            # Give the port a moment to settle
            time.sleep(0.5)
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()

            # Handshake: Try sending '?' a few times in case the first is missed
            response = ""
            for attempt in range(3):
                self._serial.write(b'?')
                self._serial.flush()
                
                # Try to read multiple lines in case there's boot-up chatter
                for _ in range(5):
                    line = self._serial.readline().decode('utf-8', errors='ignore').strip()
                    if line.startswith("STATUS"):
                        response = line
                        break
                if response:
                    break
                time.sleep(0.2)
            
            if response.startswith("STATUS"):
                parts = response.split()
                for p in parts:
                    if p.startswith("barcode="):
                        state['barcode'] = int(p.split('=')[1])
                    elif p.startswith("cam="):
                        state['cam'] = int(p.split('=')[1])
                msg = f"Arduino connected on {port}."
            else:
                # If we opened the port but got no STATUS, it might be the wrong board
                # or wrong code, but we'll allow it to proceed as 'connected' for now
                # while warning the user.
                msg = f"Connected to {port}, but no response to '?' query."

            self._serial_ok = True
            self._fire_status()
            return True, msg, state

        except serial.SerialException as e:
            self._serial_ok = False
            self._fire_status()
            err_msg = str(e)
            if "PermissionError" in err_msg or "Access is denied" in err_msg:
                return False, f"Port {port} busy. Is Serial Monitor open?", state
            return False, f"Could not open {port}: {err_msg}", state

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

    def _send(self, cmd: bytes):
        """Send a single command byte. Silently ignores if not connected."""
        if self._serial_ok and self._serial and self._serial.is_open:
            try:
                self._serial.write(cmd)
            except serial.SerialException:
                self._serial_ok = False
                self._fire_status()

    # Public command methods
    def cmd_recording_active(self):
        """'R' - triggered recording start: barcodes now, cam TTLs at next barcode boundary."""
        self._send(b'R')

    def cmd_recording_ending(self):
        """'X' - triggered recording stop: cam TTLs stop now, barcodes finish cleanly."""
        self._send(b'X')

    def cmd_start_cam_free(self):
        """'C' - free record: start cam TTLs only, no barcode gating."""
        self._send(b'C')

    def cmd_stop_cam_free(self):
        """'E' - free record stop: stop cam TTLs."""
        self._send(b'E')

    def cmd_start_barcodes(self):
        """'B' - start barcode TTLs only, without affecting camera TTLs."""
        self._send(b'B')

    def cmd_stop_barcodes(self):
        """'D' - stop barcode TTLs only, without affecting camera TTLs."""
        self._send(b'D')

    def cmd_start_all(self):
        """'A' - legacy: start barcodes + cam immediately."""
        self._send(b'A')

    def cmd_stop_all(self):
        """'S' - emergency stop everything immediately."""
        self._send(b'S')

    def query_status(self) -> str:
        """Send '?' and return the raw STATUS line (blocking, 2s timeout)."""
        if not (self._serial_ok and self._serial and self._serial.is_open):
            return "Arduino not connected"
        try:
            self._serial.reset_input_buffer()
            self._serial.write(b'?')
            return self._serial.readline().decode('utf-8', errors='ignore').strip()
        except Exception as e:
            return f"Query failed: {e}"

    @staticmethod
    def list_ports() -> list[str]:
        """Return list of available COM port names - useful for GUI port picker."""
        return [p.device for p in serial.tools.list_ports.comports()]

    @property
    def arduino_connected(self) -> bool:
        return self._serial_ok

    # -------------------------------------------------- Open Ephys polling
    def start_polling(self, on_record_start=None, on_record_stop=None,
                      on_status_change=None):
        """
        Start background thread polling Open Ephys HTTP API.
        Callbacks:
          on_record_start()        - OE just entered RECORD state
          on_record_stop()         - OE just left RECORD state
          on_status_change(state)  - any state change, state is a string
        """
        self._on_record_start  = on_record_start
        self._on_record_stop   = on_record_stop
        self._on_status_change = on_status_change
        self._oe_stop.clear()
        self._oe_state = None
        self._oe_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="OEPollThread"
        )
        self._oe_thread.start()

    def stop_polling(self):
        self._oe_stop.set()
        if self._oe_thread:
            self._oe_thread.join(timeout=3.0)

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

    def _poll_loop(self):
        host     = self._cfg['hardware']['open_ephys_host']
        port     = self._cfg['hardware']['open_ephys_port']
        interval = self._cfg['hardware']['open_ephys_poll_interval_ms'] / 1000.0
        url      = f"http://{host}:{port}/api/status"

        while not self._oe_stop.is_set():
            new_state = self._fetch_oe_state(url)
            if new_state != self._oe_state:
                prev = self._oe_state
                self._oe_state = new_state
                self._fire(self._on_status_change, new_state)
                if new_state == 'RECORD' and prev != 'RECORD':
                    self._fire(self._on_record_start)
                elif prev == 'RECORD' and new_state != 'RECORD':
                    self._fire(self._on_record_stop)
            self._oe_stop.wait(interval)

    def _fetch_oe_state(self, url: str) -> str:
        """Return OE mode string or 'UNREACHABLE' on any error."""
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                data = _json.loads(resp.read().decode())
                return data.get('mode', 'UNKNOWN').upper()
        except Exception:
            return 'UNREACHABLE'

    @property
    def oe_state(self) -> str:
        return self._oe_state or 'UNREACHABLE'

    # -------------------------------------------------------- internal helpers
    def _fire(self, callback, *args):
        """
        Schedule a callback on the tkinter main thread if we have a root,
        otherwise call directly. This keeps all GUI updates thread-safe.
        """
        if callback is None:
            return
        if self._tk:
            self._tk.after(0, lambda: callback(*args))
        else:
            callback(*args)

    def _fire_status(self):
        self._fire(self._on_status_change, self._oe_state or 'UNREACHABLE')
