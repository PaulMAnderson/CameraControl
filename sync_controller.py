# sync_controller.py
# Manages Arduino serial communication and Open Ephys HTTP polling.
# Designed to be owned by the GUI - all callbacks are fired via tkinter's
# after() mechanism so they land safely on the main thread.

import threading
import time
import serial
import serial.tools.list_ports

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

        self._oe_thread  = None
        self._oe_stop    = threading.Event()
        self._oe_state   = None          # last known OE state string

        self._on_record_start = None
        self._on_record_stop  = None
        self._on_status_change = None    # general status update -> (arduino_ok, oe_state)

    # --------------------------------------------------------- Arduino serial
    def connect_arduino(self) -> tuple[bool, str]:
        """
        Open serial connection to Arduino. Returns (success, message).
        Sends '?' and parses the STATUS response so we know current state.
        """
        port = self._cfg['hardware']['arduino_port']
        try:
            self._serial = serial.Serial(port, 9600, timeout=2.0)
            time.sleep(2.0)   # Arduino resets on serial connect - wait for boot
            self._serial.reset_input_buffer()
            self._serial.write(b'?')
            response = self._serial.readline().decode('utf-8', errors='ignore').strip()
            self._serial_ok = True
            self._fire_status()
            return True, f"Arduino connected on {port}. {response}"
        except serial.SerialException as e:
            self._serial_ok = False
            self._fire_status()
            return False, f"Arduino not found on {port}: {e}"

    def disconnect_arduino(self):
        """Send stop-all before closing so pins go low."""
        if self._serial and self._serial.is_open:
            try:
                self._serial.write(b'S')
                time.sleep(0.1)
                self._serial.close()
            except Exception:
                pass
        self._serial_ok = False

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
