# camera_capture.py
# FLIR BlackFly S - single-window camera capture application.
# Launch via launch_camera.bat (no console window).
#
# Modes:
#   Triggered   - camera driven by Arduino TTLs, recording bracketed by Open Ephys state
#   Free Record - manual start/stop, frame rate selectable
#   View Only   - live preview, nothing written to disk
#
# Thread model:
#   Main thread      : tkinter GUI only
#   camCaptureThread : GetNextImage loop -> writeQueue (direct) + previewQueue (non-blocking)
#   saveThread       : writeQueue -> ffmpeg writer
#   OE poll thread   : HTTP polling (owned by SyncController, daemon)

import os
os.environ['FOR_DISABLE_CONSOLE_CTRL_HANDLER'] = '1'   # prevent ctrl-c crash on Windows

import sys
import json
import time
import queue
import atexit
import threading
import traceback
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

# ------------------------------------------------------------------ logging
# Redirect stdout/stderr to a log file BEFORE any other imports, so even
# import errors and missing-package crashes are captured.
# Log is written next to this script as: camera_capture.log
_LOG_PATH = Path(__file__).parent / "camera_capture.log"

def _setup_logging():
    log = open(_LOG_PATH, 'w', buffering=1)  # line-buffered: writes land immediately
    sys.stdout = log
    sys.stderr = log
    print(f"=== camera_capture starting {datetime.now().isoformat()} ===")
    print(f"Python:      {sys.version}")
    print(f"Executable:  {sys.executable}")
    print(f"Working dir: {os.getcwd()}")
    print(f"Script dir:  {Path(__file__).parent}")
    return log

_log_file = _setup_logging()

print("Importing tkinter...")
import tkinter as tk
from tkinter import ttk, messagebox
print("  tkinter OK")

print("Importing numpy / PIL...")
import numpy as np
from PIL import Image, ImageTk
print("  numpy / PIL OK")

print("Importing PySpin...")
import PySpin
print("  PySpin OK")

print("Importing skvideo...")
import skvideo
# ffmpeg path set after config load (see load_config)
import skvideo.io
print("  skvideo OK")

print("Importing SyncController...")
from sync_controller import SyncController
print("  SyncController OK")


# ============================================================= state machine
class AppState(Enum):
    IDLE       = auto()   # camera running, no recording
    ARMED      = auto()   # triggered mode: waiting for OE to start
    RECORDING  = auto()   # actively writing frames to disk
    FINISHING  = auto()   # stop requested, draining write queue


class CaptureMode(Enum):
    TRIGGERED   = "triggered"
    FREE_RECORD = "free"
    VIEW_ONLY   = "view"


# ================================================================ config load
CONFIG_PATH = Path(__file__).parent / "config.json"

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        messagebox.showerror(
            "Missing config",
            f"config.json not found at:\n{CONFIG_PATH}\n\nPlease create it before running."
        )
        sys.exit(1)
    with open(CONFIG_PATH, 'r') as f:
        cfg = json.load(f)
    # Set ffmpeg path before skvideo is used
    skvideo.setFFmpegPath(cfg['paths']['ffmpeg_path'])
    return cfg


# ============================================================= metadata helpers
def make_metadata_stub(cfg: dict, animal_id: str, mode: CaptureMode,
                       fps: int, filepath: str) -> dict:
    """Initial metadata written at recording START. Status = 'recording'."""
    now = datetime.now()
    return {
        "status": "recording",
        "recording": {
            "animal_id":       animal_id,
            "mode":            mode.value,
            "fps":             fps,
            "start_time":      now.isoformat(),
            "end_time":        None,
            "duration_sec":    None,
            "frames_captured": None,
            "frames_dropped":  None,
            "frames_saved":    None,
        },
        "rig": {
            "room":           cfg['rig']['room'],
            "rig_id":         cfg['rig']['rig_id'],
        },
        "video": {
            "filename":  Path(filepath).name,
            "filepath":  filepath,
            "codec":     cfg['encoding']['codec'],
            "codec_tag": cfg['encoding']['codec_tag'],
            "preset":    cfg['encoding']['preset'],
            "resolution": [cfg['camera']['width'], cfg['camera']['height']],
        },
        "sync": {
            "arduino_port":      cfg['hardware']['arduino_port'],
            "open_ephys_host":   cfg['hardware']['open_ephys_host'],
        }
    }


def finalise_metadata(stub: dict, end_time: datetime, frames_captured: int,
                       frames_dropped: int, frames_saved: int) -> dict:
    """Fill in end-of-recording fields and mark status complete."""
    start = datetime.fromisoformat(stub['recording']['start_time'])
    stub['status'] = 'complete'
    stub['recording']['end_time']       = end_time.isoformat()
    stub['recording']['duration_sec']   = round((end_time - start).total_seconds(), 3)
    stub['recording']['frames_captured'] = frames_captured
    stub['recording']['frames_dropped']  = frames_dropped
    stub['recording']['frames_saved']    = frames_saved
    return stub


def write_metadata(filepath: str, meta: dict):
    """Write metadata JSON alongside the video file."""
    json_path = Path(filepath).with_suffix('.json')
    with open(json_path, 'w') as f:
        json.dump(meta, f, indent=4)


# ================================================================ camera init
def init_cam(cam, cfg: dict, fps: int, triggered: bool):
    """Initialise BlackFly S with settings from config."""
    cam.Init()
    cam.UserSetSelector.SetValue(PySpin.UserSetSelector_Default)
    cam.UserSetLoad()

    cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)
    cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
    cam.ExposureMode.SetValue(PySpin.ExposureMode_Timed)
    cam.ExposureTime.SetValue(cfg['camera']['exposure_us'])

    cam.GainAuto.SetValue(PySpin.GainAuto_Off)
    cam.Gain.SetValue(cfg['camera']['gain_db'])
    cam.GammaEnable.SetValue(True)
    cam.Gamma.SetValue(cfg['camera']['gamma'])

    cam.AdcBitDepth.SetValue(PySpin.AdcBitDepth_Bit10)
    cam.PixelFormat.SetValue(PySpin.PixelFormat_Mono8)
    cam.Width.SetValue(cfg['camera']['width'])
    cam.Height.SetValue(cfg['camera']['height'])
    cam.OffsetX.SetValue(0)
    cam.OffsetY.SetValue(0)

    # Stream buffer
    tl = cam.GetTLStreamNodeMap()
    hm = PySpin.CEnumerationPtr(tl.GetNode('StreamBufferHandlingMode'))
    hm.SetIntValue(hm.GetEntryByName(cfg['camera']['buffer_mode']).GetValue())
    bc = PySpin.CIntegerPtr(tl.GetNode('StreamBufferCountManual'))
    if PySpin.IsAvailable(bc) and PySpin.IsWritable(bc):
        bc.SetValue(cfg['camera']['buffer_count'])
    bcm = PySpin.CEnumerationPtr(tl.GetNode('StreamBufferCountMode'))
    if PySpin.IsAvailable(bcm) and PySpin.IsWritable(bcm):
        e = bcm.GetEntryByName('Manual')
        if PySpin.IsAvailable(e):
            bcm.SetIntValue(e.GetValue())

    cam.AcquisitionFrameRateEnable.SetValue(True)
    cam.AcquisitionFrameRate.SetValue(float(fps))

    if triggered:
        cam.TriggerMode.SetValue(PySpin.TriggerMode_On)
        cam.TriggerSource.SetValue(PySpin.TriggerSource_Line0)
    else:
        cam.TriggerMode.SetValue(PySpin.TriggerMode_Off)

    cam.LineSelector.SetValue(PySpin.LineSelector_Line1)
    cam.LineMode.SetValue(PySpin.LineMode_Output)
    cam.LineSource.SetValue(PySpin.LineSource_ExposureActive)


def make_writer(filepath: str, cfg: dict, fps: int):
    """Create skvideo FFmpegWriter from config encoding settings."""
    enc = cfg['encoding']
    outputdict = {
        '-vcodec':  enc['codec'],
        '-preset':  enc['preset'],
        '-r':       str(fps),
        '-tag:v':   enc['codec_tag'],
    }
    outputdict.update(enc.get('extra_output_flags', {}))
    return skvideo.io.FFmpegWriter(
        filepath,
        inputdict={'-framerate': str(fps)},
        outputdict=outputdict
    )


# ============================================================= capture thread
def cam_capture_thread(cam, write_queue, preview_queue, frame_stats,
                        stop_event, ready_event, cfg):
    """
    Runs in its own thread. Pulls frames from camera, pushes to write_queue
    (every frame) and preview_queue (non-blocking, drops ok).
    frame_stats dict is updated live for GUI display.
    """
    timeout_ms = cfg['camera']['cam_timeout_ms']
    h = cfg['camera']['height']
    w = cfg['camera']['width']
    shape = (h, w)
    last_id = -1

    try:
        ready_event.set()
        while not stop_event.is_set():
            try:
                image = cam.GetNextImage(timeout_ms)
            except PySpin.SpinnakerException:
                if stop_event.is_set():
                    break
                frame_stats['timeouts'] += 1
                continue

            if image.IsIncomplete():
                image.Release()
                continue

            fid = image.GetFrameID()
            if last_id >= 0 and fid != last_id + 1:
                frame_stats['dropped'] += fid - (last_id + 1)
            last_id = fid

            npimg = np.frombuffer(image.GetData(), dtype=np.uint8).reshape(shape).copy()
            image.Release()

            if write_queue is not None:
                write_queue.put(npimg)
            frame_stats['captured'] += 1

            try:
                preview_queue.put_nowait(npimg)
            except queue.Full:
                pass

    except Exception as e:
        frame_stats['error'] = str(e)
        traceback.print_exc()
    finally:
        frame_stats['capture_done'] = True


# ================================================================ save thread
def save_thread_func(write_queue, writer, frame_stats, stop_event):
    """
    Runs in its own thread. Drains write_queue into ffmpeg writer.
    Exits when it receives None sentinel.
    """
    saved = 0
    try:
        while True:
            try:
                item = write_queue.get(timeout=1.0)
            except queue.Empty:
                if stop_event.is_set() and frame_stats.get('capture_done'):
                    break
                continue
            if item is None:
                write_queue.task_done()
                break
            writer.writeFrame(item)
            write_queue.task_done()
            saved += 1
    finally:
        frame_stats['saved'] = saved


# ================================================================= main GUI
class CameraApp:

    PREVIEW_INTERVAL_MS = 66   # ~15 fps preview update

    def __init__(self, root: tk.Tk, cfg: dict):
        self.root  = root
        self.cfg   = cfg
        self.state = AppState.IDLE
        self.mode  = CaptureMode[self.cfg['gui']['default_mode'].upper()]

        # Camera handles
        self._spin_system  = None
        self._cam_list     = None
        self._cam          = None

        # Threading
        self._capture_thread = None
        self._save_thread    = None
        self._write_queue    = None
        self._preview_queue  = queue.Queue(maxsize=1)
        self._stop_event     = threading.Event()
        self._ready_event    = threading.Event()
        self._frame_stats    = {}

        # Recording state
        self._writer       = None
        self._filepath     = None
        self._metadata     = None
        self._rec_start    = None

        # OR Logic: sources with active start requests
        self._active_sources   = set()
        self._barcodes_running = False    # mirrors Arduino barcode state for button toggle

        # Sync
        self.sync = SyncController(cfg, tk_root=root)

        print("Building GUI...")
        self._build_gui()
        print("GUI built OK")
        print("Initialising camera...")
        self._init_camera()
        print("Camera initialised OK")
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

        # Start preview loop
        self.root.after(self.PREVIEW_INTERVAL_MS, self._update_preview)

        # Safe shutdown on window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        atexit.register(self._cleanup)
        print("CameraApp __init__ complete")

    # ------------------------------------------------------------ GUI build
    def _build_gui(self):
        self.root.title("FLIR Camera Capture")
        self.root.resizable(False, False)

        # ── left: preview canvas ──────────────────────────────────────────
        left = tk.Frame(self.root, bg='black')
        left.grid(row=0, column=0, rowspan=3, padx=(8,4), pady=8, sticky='nsew')

        w = self.cfg['camera']['width']
        h = self.cfg['camera']['height']
        self.canvas = tk.Canvas(left, width=w, height=h, bg='black',
                                highlightthickness=0)
        self.canvas.pack()
        self._preview_image_id = None

        # ── right panel ───────────────────────────────────────────────────
        right = tk.Frame(self.root, padx=8, pady=8)
        right.grid(row=0, column=1, sticky='nsew')

        # Animal ID
        tk.Label(right, text="Animal ID:", anchor='w').grid(
            row=0, column=0, sticky='w', pady=(0,2))
        self.animal_id_var = tk.StringVar()
        self.animal_entry = tk.Entry(right, textvariable=self.animal_id_var, width=20)
        self.animal_entry.grid(row=1, column=0, sticky='w', pady=(0,8))

        # Mode
        tk.Label(right, text="Mode:", anchor='w').grid(
            row=2, column=0, sticky='w', pady=(0,2))
        self.mode_var = tk.StringVar(value=self.mode.value)
        modes = [
            ("Triggered (default)",  CaptureMode.TRIGGERED.value),
            ("Free Record",          CaptureMode.FREE_RECORD.value),
            ("View Only",            CaptureMode.VIEW_ONLY.value),
        ]
        for i, (label, val) in enumerate(modes):
            rb = tk.Radiobutton(right, text=label, variable=self.mode_var,
                                value=val, command=self._on_mode_change)
            rb.grid(row=3+i, column=0, sticky='w')

        # FPS (free record only)
        fps_frame = tk.Frame(right)
        fps_frame.grid(row=6, column=0, sticky='w', pady=(6,0))
        tk.Label(fps_frame, text="FPS:").pack(side='left')
        self.fps_var = tk.StringVar(value=str(self.cfg['camera']['triggered_fps']))
        fps_opts = [str(x) for x in self.cfg['gui']['free_record_fps_options']]
        self.fps_menu = ttk.Combobox(fps_frame, textvariable=self.fps_var,
                                     values=fps_opts, width=6, state='disabled')
        self.fps_menu.pack(side='left', padx=4)
        self.fps_note = tk.Label(fps_frame, text="(set on Arduino in triggered mode)",
                                 fg='grey', font=('TkDefaultFont', 8))
        self.fps_note.pack(side='left')

        ttk.Separator(right, orient='horizontal').grid(
            row=7, column=0, sticky='ew', pady=8)

        # Status indicators
        status_frame = tk.Frame(right)
        status_frame.grid(row=8, column=0, sticky='w')
        self._oe_dot    = tk.Label(status_frame, text="●", fg='grey', font=('TkDefaultFont', 12))
        self._oe_dot.grid(row=0, column=0, sticky='w')
        self._oe_label  = tk.Label(status_frame, text="Open Ephys: connecting...")
        self._oe_label.grid(row=0, column=1, sticky='w', padx=4)
        self._ard_dot   = tk.Label(status_frame, text="●", fg='grey', font=('TkDefaultFont', 12))
        self._ard_dot.grid(row=1, column=0, sticky='w')
        self._ard_label = tk.Label(status_frame, text="Arduino: connecting...")
        self._ard_label.grid(row=1, column=1, sticky='w', padx=4)

        ttk.Separator(right, orient='horizontal').grid(
            row=9, column=0, sticky='ew', pady=8)

        # Stats
        stats_frame = tk.Frame(right)
        stats_frame.grid(row=10, column=0, sticky='w')
        self._stat_frame_lbl   = tk.Label(stats_frame, text="Frame:   0",   anchor='w', width=28)
        self._stat_elapsed_lbl = tk.Label(stats_frame, text="Elapsed: --",  anchor='w', width=28)
        self._stat_dropped_lbl = tk.Label(stats_frame, text="Dropped: 0",   anchor='w', width=28)
        self._stat_queue_lbl   = tk.Label(stats_frame, text="Write queue: 0", anchor='w', width=28)
        for i, lbl in enumerate([self._stat_frame_lbl, self._stat_elapsed_lbl,
                                  self._stat_dropped_lbl, self._stat_queue_lbl]):
            lbl.grid(row=i, column=0, sticky='w')

        ttk.Separator(right, orient='horizontal').grid(
            row=11, column=0, sticky='ew', pady=8)

        # Buttons
        btn_frame = tk.Frame(right)
        btn_frame.grid(row=12, column=0, sticky='ew')
        self.record_btn = tk.Button(btn_frame, text="Record", width=12,
                                    bg='#4CAF50', fg='white', font=('TkDefaultFont', 10, 'bold'),
                                    command=self._on_record)
        self.record_btn.pack(side='left', padx=(0,4))
        self.stop_btn = tk.Button(btn_frame, text="Stop", width=12,
                                  bg='#f44336', fg='white', font=('TkDefaultFont', 10, 'bold'),
                                  state='disabled', command=self._on_stop)
        self.stop_btn.pack(side='left')

        # File path label
        self._file_label = tk.Label(right, text="File: --", anchor='w',
                                    fg='grey', font=('TkDefaultFont', 8),
                                    wraplength=280, justify='left')
        self._file_label.grid(row=13, column=0, sticky='w', pady=(6,0))

        # State label (bottom, full width)
        self._state_label = tk.Label(self.root, text="● IDLE",
                                     font=('TkDefaultFont', 9), anchor='w', fg='grey')
        self._state_label.grid(row=2, column=0, columnspan=2, sticky='w', padx=8, pady=(0,6))

        self._on_mode_change()

    # --------------------------------------------------- mode change handler
    def _on_mode_change(self):
        self.mode = CaptureMode(self.mode_var.get())
        if self.mode == CaptureMode.FREE_RECORD:
            self.fps_menu.config(state='readonly')
            self.fps_note.config(text="")
            self.fps_var.set(str(self.cfg['gui']['free_record_fps_options'][-1]))
        else:
            self.fps_menu.config(state='disabled')
            self.fps_note.config(text="(set on Arduino in triggered mode)")
            self.fps_var.set(str(self.cfg['camera']['triggered_fps']))

    # --------------------------------------------------- camera init
    def _init_camera(self):
        try:
            self._spin_system = PySpin.System.GetInstance()
            self._cam_list    = self._spin_system.GetCameras()
            if self._cam_list.GetSize() == 0:
                messagebox.showerror("No Camera", "No FLIR camera detected.")
                sys.exit(1)
            self._cam = self._cam_list[0]
            fps = self.cfg['camera']['triggered_fps']
            triggered = (self.mode == CaptureMode.TRIGGERED)
            init_cam(self._cam, self.cfg, fps, triggered)
            self._start_preview_capture()
        except Exception as e:
            messagebox.showerror("Camera Error", f"Failed to initialise camera:\n{e}")
            sys.exit(1)

    def _start_preview_capture(self):
        """Start the capture thread in free-run mode (preview only, no writer).
        Always free-run regardless of selected mode so preview works without TTLs."""
        print("_start_preview_capture: configuring camera for free-run preview...")
        fps = self.cfg['camera']['triggered_fps']
        init_cam(self._cam, self.cfg, fps, triggered=False)
        self._reset_frame_stats()
        self._stop_event.clear()
        self._ready_event.clear()
        print("_start_preview_capture: BeginAcquisition...")
        self._cam.BeginAcquisition()
        print("_start_preview_capture: starting capture thread...")
        self._capture_thread = threading.Thread(
            target=cam_capture_thread,
            args=(self._cam, None, self._preview_queue,
                  self._frame_stats, self._stop_event, self._ready_event, self.cfg),
            daemon=True, name="CamCaptureThread"
        )
        self._capture_thread.start()
        self._ready_event.wait(timeout=3.0)
        print("_start_preview_capture: capture thread ready")

    def _stop_capture_thread(self):
        print("_stop_capture_thread: signalling stop...")
        self._stop_event.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=5.0)
        print("_stop_capture_thread: capture thread stopped, ending acquisition...")
        try:
            self._cam.EndAcquisition()
            print("_stop_capture_thread: EndAcquisition OK")
        except PySpin.SpinnakerException as e:
            print(f"_stop_capture_thread: EndAcquisition skipped ({e})")
        except Exception as e:
            print(f"_stop_capture_thread: EndAcquisition error ({e})")

    # --------------------------------------------------- Arduino connect
    def _connect_arduino(self):
        ok, msg = self.sync.connect_arduino()
        self._update_arduino_indicator(ok, msg)

    def _update_arduino_indicator(self, ok: bool, msg: str = ""):
        if ok:
            self._ard_dot.config(fg='#4CAF50')
            self._ard_label.config(text="Arduino: Connected")
        else:
            self._ard_dot.config(fg='#f44336')
            self._ard_label.config(text=f"Arduino: Not found")

    # --------------------------------------------------- OE callbacks
    def _oe_status_changed(self, state: str):
        colours = {
            'RECORD':      '#f44336',
            'ACQUIRE':     '#FF9800',
            'IDLE':        '#4CAF50',
            'UNREACHABLE': 'grey',
        }
        col = colours.get(state, 'grey')
        self._oe_dot.config(fg=col)
        self._oe_label.config(text=f"Open Ephys: {state}")

    def _oe_record_started(self):
        """Open Ephys just started recording — trigger capture if in TRIGGERED mode."""
        if self.mode != CaptureMode.TRIGGERED:
            return
        if self.state not in (AppState.IDLE, AppState.ARMED):
            return
        self._trigger_start('oe')

    def _oe_record_stopped(self):
        """Open Ephys just stopped recording.
        Intentional no-op: Python is OE's master.
        The video continues until the last active source (Matlab or button) stops.
        Python sends the STOP command to OE in _finish_worker after the writer closes.
        """
        pass

    # --------------------------------------------------- OR Logic trigger routing
    def _trigger_start(self, source: str):
        """Register a source requesting recording start.
        Starts recording on first call; subsequent calls from other sources just
        add to _active_sources without restarting.

        Callers must gate on TRIGGERED mode before calling this method.
        The method applies when state is IDLE or ARMED.
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

    # --------------------------------------------------- Arduino and Matlab callbacks

    def _matlab_started(self):
        """Matlab sent a UDP 'START' — request recording start (TRIGGERED mode only)."""
        if self.mode != CaptureMode.TRIGGERED:
            return
        self._trigger_start('matlab')

    def _matlab_stopped(self):
        """Matlab sent a UDP 'STOP' — release matlab's recording request."""
        if self.mode != CaptureMode.TRIGGERED:
            return
        self._trigger_stop('matlab')

    # --------------------------------------------------- record / stop buttons
    def _on_record(self):
        if self.state != AppState.IDLE:
            return

        animal = self.animal_id_var.get().strip()
        if not animal:
            messagebox.showwarning("Animal ID", "Please enter an Animal ID before recording.")
            return

        if self.mode == CaptureMode.VIEW_ONLY:
            messagebox.showinfo("View Only", "Switch to Triggered or Free Record to save video.")
            return

        if self.mode == CaptureMode.TRIGGERED:
            # Arm: tell Arduino to start barcodes, wait for OE record signal
            self.sync.cmd_recording_active()
            self.state = AppState.ARMED
            self._set_state_label("● ARMED — waiting for Open Ephys...", '#FF9800')
            self.record_btn.config(state='disabled')
            self.stop_btn.config(state='normal')
            # If OE is already recording (e.g. user clicked Record after OE started)
            if self.sync.oe_state == 'RECORD':
                self._begin_recording()

        elif self.mode == CaptureMode.FREE_RECORD:
            self.sync.cmd_start_cam_free()
            self._begin_recording()

    def _on_stop(self):
        self._active_sources.clear()   # manual override: cancel all external requests
        if self.state == AppState.ARMED:
            self.sync.cmd_recording_ending()
            # _barcodes_running is not set during ARMED, no reset needed here
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
                # _recording_finished resets _barcodes_running after writer closes
            self._end_recording()

    # --------------------------------------------------- recording lifecycle
    def _make_filepath(self, animal: str) -> str:
        now      = datetime.now()
        date_str = now.strftime("%Y_%m_%d")
        time_str = now.strftime("_%H_%M_%S")
        folder   = Path(self.cfg['paths']['save_folder']) / animal
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{animal}_{date_str}{time_str}.mp4"
        return str(folder / filename)

    def _begin_recording(self):
        """Transition IDLE/ARMED -> RECORDING. Creates writer and save thread."""
        animal   = self.animal_id_var.get().strip()
        fps      = int(self.fps_var.get())
        filepath = self._make_filepath(animal)

        self._filepath    = filepath
        self._rec_start   = datetime.now()
        self._metadata    = make_metadata_stub(self.cfg, animal, self.mode, fps, filepath)
        write_metadata(filepath, self._metadata)   # crash-safe stub written NOW

        # Stop current preview-only capture, reconfigure camera, restart with writer
        self._stop_capture_thread()
        triggered = (self.mode == CaptureMode.TRIGGERED)
        init_cam(self._cam, self.cfg, fps, triggered)

        # Build write queue and writer
        self._write_queue = queue.Queue()
        self._reset_frame_stats()
        self._stop_event.clear()
        self._ready_event.clear()

        self._writer = make_writer(filepath, self.cfg, fps)

        # Start save thread
        save_stop = threading.Event()
        self._save_stop_event = save_stop
        self._save_thread = threading.Thread(
            target=save_thread_func,
            args=(self._write_queue, self._writer, self._frame_stats, save_stop),
            daemon=True, name="SaveThread"
        )
        self._save_thread.start()

        # Start capture thread (now writes to write_queue)
        self._cam.BeginAcquisition()
        self._capture_thread = threading.Thread(
            target=cam_capture_thread,
            args=(self._cam, self._write_queue, self._preview_queue,
                  self._frame_stats, self._stop_event, self._ready_event, self.cfg),
            daemon=True, name="CamCaptureThread"
        )
        self._capture_thread.start()
        self._ready_event.wait(timeout=3.0)

        self.state = AppState.RECORDING
        self._set_state_label("● RECORDING", '#f44336')
        self.record_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.animal_entry.config(state='disabled')
        self._file_label.config(text=f"File: {Path(filepath).name}", fg='black')

    def _end_recording(self):
        """Transition RECORDING -> FINISHING -> IDLE."""
        self.state = AppState.FINISHING
        self._set_state_label("● FINISHING — writing to disk...", '#FF9800')
        self.stop_btn.config(state='disabled')

        # Run shutdown in background so GUI stays responsive
        threading.Thread(target=self._finish_worker, daemon=True,
                         name="FinishThread").start()

    def _finish_worker(self):
        """Background: drain queues, close writer, write final metadata."""
        # Signal capture thread to stop
        self._stop_event.set()
        if self._capture_thread:
            self._capture_thread.join(timeout=5.0)

        # Signal save thread - send None sentinel after queue is drained
        if self._write_queue:
            self._write_queue.put(None)
        if self._save_thread:
            self._save_thread.join(timeout=60.0)

        # Close writer
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None

        # Finalise metadata
        end_time = datetime.now()
        meta = finalise_metadata(
            self._metadata, end_time,
            self._frame_stats.get('captured', 0),
            self._frame_stats.get('dropped',  0),
            self._frame_stats.get('saved',    0),
        )
        write_metadata(self._filepath, meta)

        # Tell Open Ephys to stop recording — Python is master, OE is slave (AC2.1)
        # Called after writer closes so the ephys file captures the final barcode
        self.sync.cmd_stop_oe_recording()

        # Restart preview-only capture (init_cam called inside _start_preview_capture)
        print("_finish_worker: restarting preview capture...")
        self._start_preview_capture()
        print("_finish_worker: preview restarted OK")

        # Back to GUI thread
        self.root.after(0, self._recording_finished)

    def _recording_finished(self):
        self._active_sources.clear()      # Clear stale sources when recording ends
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

    # --------------------------------------------------- preview loop
    def _update_preview(self):
        """Called every PREVIEW_INTERVAL_MS on main thread. Updates canvas + stats."""
        try:
            frame = self._preview_queue.get_nowait()
            img   = Image.fromarray(frame)
            photo = ImageTk.PhotoImage(img)
            if self._preview_image_id is None:
                self._preview_image_id = self.canvas.create_image(0, 0, anchor='nw', image=photo)
            else:
                self.canvas.itemconfig(self._preview_image_id, image=photo)
            self.canvas._photo = photo   # keep reference
        except queue.Empty:
            pass

        # Update stats
        captured = self._frame_stats.get('captured', 0)
        dropped  = self._frame_stats.get('dropped',  0)
        qsize    = self._write_queue.qsize() if self._write_queue else 0

        self._stat_frame_lbl.config(text=f"Frame:   {captured:,}")
        self._stat_dropped_lbl.config(
            text=f"Dropped: {dropped}",
            fg='#f44336' if dropped > 0 else 'black'
        )
        self._stat_queue_lbl.config(text=f"Write queue: {qsize}")

        if self.state == AppState.RECORDING and self._rec_start:
            elapsed = (datetime.now() - self._rec_start).total_seconds()
            self._stat_elapsed_lbl.config(text=f"Elapsed: {elapsed:.1f} s")
        else:
            self._stat_elapsed_lbl.config(text="Elapsed: --")

        self.root.after(self.PREVIEW_INTERVAL_MS, self._update_preview)

    # --------------------------------------------------- helpers
    def _reset_frame_stats(self):
        self._frame_stats = {
            'captured':     0,
            'dropped':      0,
            'saved':        0,
            'timeouts':     0,
            'capture_done': False,
            'error':        None,
        }

    def _set_state_label(self, text: str, colour: str):
        self._state_label.config(text=text, fg=colour)

    # --------------------------------------------------- shutdown
    def _on_close(self):
        if self.state in (AppState.RECORDING, AppState.FINISHING):
            if not messagebox.askyesno(
                "Recording in progress",
                "A recording is in progress. Stop and exit?"
            ):
                return
            if self.state == AppState.RECORDING:
                self._on_stop()
                # Give finish worker a moment before hard exit
                self.root.after(2000, self._cleanup_and_destroy)
                return
        self._cleanup_and_destroy()

    def _cleanup_and_destroy(self):
        self._cleanup()
        self.root.destroy()

    def _cleanup(self):
        """Safe shutdown - always drives Arduino pins low and releases camera."""
        print("_cleanup: starting...")
        try:
            self.sync.stop_polling()
            self.sync.stop_udp_listener()
            self.sync.disconnect_arduino()
            print("_cleanup: sync stopped")
        except Exception as e:
            print(f"_cleanup: sync error ({e})")

        # Stop any running writer
        if self._writer:
            try:
                self._writer.close()
                print("_cleanup: writer closed")
            except Exception as e:
                print(f"_cleanup: writer close error ({e})")
            self._writer = None

        # Stop capture thread
        self._stop_event.set()
        try:
            if self._capture_thread and self._capture_thread.is_alive():
                self._capture_thread.join(timeout=3.0)
            print("_cleanup: capture thread stopped")
        except Exception as e:
            print(f"_cleanup: capture thread error ({e})")

        # Release camera - order matters: EndAcquisition -> DeInit -> Clear -> Release
        try:
            if self._cam:
                try:
                    self._cam.EndAcquisition()
                    print("_cleanup: EndAcquisition OK")
                except PySpin.SpinnakerException:
                    print("_cleanup: EndAcquisition skipped (not acquiring)")
                try:
                    self._cam.DeInit()
                    print("_cleanup: DeInit OK")
                except Exception as e:
                    print(f"_cleanup: DeInit error ({e})")
                del self._cam
                self._cam = None
            if self._cam_list:
                self._cam_list.Clear()
                print("_cleanup: cam_list cleared")
            if self._spin_system:
                self._spin_system.ReleaseInstance()
                print("_cleanup: system released")
        except Exception as e:
            print(f"_cleanup: camera teardown error ({e})")
        print("_cleanup: done")


# ================================================================= entry point
def main():
    print("main() starting...")
    print("Loading config...")
    cfg  = load_config()
    print("Config loaded OK")
    print("Creating tkinter root...")
    root = tk.Tk()
    print("tkinter root created OK")

    # Top-level exception handler - shows dialog AND logs instead of silent crash
    def handle_exception(exc_type, exc_value, exc_tb):
        msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        print(f"UNHANDLED EXCEPTION:\n{msg}")
        _log_file.flush()
        messagebox.showerror("Unexpected Error", msg)
        try:
            root.destroy()
        except Exception:
            pass
    sys.excepthook = handle_exception

    try:
        print("Creating CameraApp...")
        app = CameraApp(root, cfg)
        print("CameraApp created OK - entering mainloop")
        _log_file.flush()
        root.mainloop()
        print("mainloop exited cleanly")
    except Exception as e:
        msg = traceback.format_exc()
        print(f"CRASH during startup or mainloop:\n{msg}")
        _log_file.flush()
        try:
            messagebox.showerror("Startup Error", msg)
        except Exception:
            pass


if __name__ == '__main__':
    main()
