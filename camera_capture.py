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
_LOG_PATH = Path(__file__).parent / "camera_capture.log"

def _setup_logging():
    log = open(_LOG_PATH, 'w', buffering=1)
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
import skvideo.io
print("  skvideo OK")

print("Importing SyncController...")
from sync_controller import SyncController
print("  SyncController OK")


# ============================================================= state machine
class AppState(Enum):
    IDLE       = auto()   # camera running, no recording
    ARMED      = auto()   # triggered mode: waiting for trigger
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
            "frames_captured": None,
            "frames_dropped":  None,
        },
        "rig": {
            "room":           cfg['rig']['room'],
            "rig_id":         cfg['rig']['rig_id'],
        },
        "video": {
            "filename":   Path(filepath).name,
            "camera":     cfg['camera'].get('label', 'Cam1'),
            "resolution": [cfg['camera']['width'], cfg['camera']['height']],
            "codec":      cfg['encoding']['codec'],
            "codec_tag":  cfg['encoding']['codec_tag'],
        }
    }


def finalise_metadata(stub: dict, end_time: datetime, frames_captured: int,
                       frames_dropped: int, frames_saved: int) -> dict:
    """Fill in end-of-recording fields and mark status complete."""
    stub['status'] = 'complete'
    stub['recording']['end_time']       = end_time.isoformat()
    stub['recording']['frames_captured'] = frames_captured
    stub['recording']['frames_dropped']  = frames_dropped
    return stub


def write_metadata(filepath: str, meta: dict):
    """Write metadata JSON alongside the video file."""
    json_path = Path(filepath).with_suffix('.json')
    with open(json_path, 'w') as f:
        json.dump(meta, f, indent=4)


# ================================================================ camera init
def init_cam(cam, cfg: dict, fps: int, triggered: bool, enable_output: bool = True):
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

    # Line 1 output configuration
    try:
        cam.LineSelector.SetValue(PySpin.LineSelector_Line1)
        if PySpin.IsAvailable(cam.LineMode) and PySpin.IsWritable(cam.LineMode):
            cam.LineMode.SetValue(PySpin.LineMode_Output)
        
        if PySpin.IsAvailable(cam.LineSource) and PySpin.IsWritable(cam.LineSource):
            if enable_output:
                cam.LineSource.SetValue(PySpin.LineSource_ExposureActive)
            else:
                try:
                    cam.LineSource.SetValue(PySpin.LineSource_UserOutput0)
                except PySpin.SpinnakerException:
                    try:
                        cam.LineSource.SetValue(PySpin.LineSource_Off)
                    except PySpin.SpinnakerException:
                        pass
    except Exception as e:
        print(f"Warning: Line 1 (Exposure TTL) configuration failed: {e}")



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
                        stop_event, ready_event, cfg, app_ref=None):
    """
    Runs in its own thread. Pulls frames from camera, pushes to write_queue.
    If app_ref is provided and state is ARMED, transitions to RECORDING on first frame.
    """
    timeout_ms = cfg['camera']['cam_timeout_ms']
    h = cfg['camera']['height']
    w = cfg['camera']['width']
    shape = (h, w)
    last_id = -1

    try:
        # --- PHASE 1: AGGRESSIVE BUFFER FLUSH ---
        # Discard stale frames until the buffer is truly empty.
        # We loop until GetNextImage times out or we've done it many times.
        for _ in range(20):
            try:
                image = cam.GetNextImage(50)
                image.Release()
            except PySpin.SpinnakerException:
                break
        
        # Now we are truly ready
        ready_event.set()

        # --- PHASE 2: CAPTURE LOOP ---
        while not stop_event.is_set():
            try:
                image = cam.GetNextImage(timeout_ms)
            except PySpin.SpinnakerException:
                if stop_event.is_set():
                    break
                # ARMED state: timeouts are expected while waiting for trigger
                frame_stats['timeouts'] += 1
                continue

            if image.IsIncomplete():
                image.Release()
                continue

            # --- Automatic State Transition ---
            if app_ref and app_ref.state == AppState.ARMED:
                # First frame received! Transition to RECORDING on main thread
                app_ref.root.after(0, app_ref._transition_to_recording)

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

    PREVIEW_INTERVAL_MS = 40

    def __init__(self, root: tk.Tk, cfg: dict):
        self.root  = root
        self.cfg   = cfg
        self.state = AppState.IDLE
        self.mode  = CaptureMode[self.cfg['gui']['default_mode'].upper()]

        self._spin_system  = None
        self._cam_list     = None
        self._cam          = None

        self._capture_thread = None
        self._save_thread    = None
        self._write_queue    = None
        self._preview_queue  = queue.Queue(maxsize=1)
        self._stop_event     = threading.Event()
        self._ready_event    = threading.Event()
        self._frame_stats    = {}

        self._writer       = None
        self._filepath     = None
        self._metadata     = None
        self._rec_start    = None
        self._hardware_triggers_started = False

        self._active_sources   = set()
        self._barcodes_running = False

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

        self.root.after(self.PREVIEW_INTERVAL_MS, self._update_preview)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        atexit.register(self._cleanup)
        print("CameraApp __init__ complete")

    # ------------------------------------------------------------ GUI build
    def _build_gui(self):
        self.root.title("FLIR Camera Capture")
        self.root.resizable(False, False)

        # ── GLOBAL STATUS BAR (Top) ───────────────────────────────────────
        self.global_bar = tk.Frame(self.root, padx=12, pady=8, relief='groove', borderwidth=1)
        self.global_bar.pack(fill='x', side='top')

        self._ard_dot   = tk.Label(self.global_bar, text="●", fg='grey', font=('TkDefaultFont', 12))
        self._ard_dot.pack(side='left')
        self._ard_label = tk.Label(self.global_bar, text="Arduino: Disconnected", font=('TkDefaultFont', 9, 'bold'))
        self._ard_label.pack(side='left', padx=(0, 20))

        self._oe_dot    = tk.Label(self.global_bar, text="●", fg='grey', font=('TkDefaultFont', 12))
        self._oe_dot.pack(side='left')
        self._oe_label  = tk.Label(self.global_bar, text="Open Ephys: ???", font=('TkDefaultFont', 9, 'bold'))
        self._oe_label.pack(side='left', padx=(0, 20))

        self._barcode_dot = tk.Label(self.global_bar, text="●", fg='grey', font=('TkDefaultFont', 12))
        self._barcode_dot.pack(side='left')
        self._barcode_label = tk.Label(self.global_bar, text="Barcodes: OFF", font=('TkDefaultFont', 9, 'bold'))
        self._barcode_label.pack(side='left', padx=(0, 10))
        
        self.barcode_btn = tk.Button(self.global_bar, text="START BARCODES", 
                                     bg='#4CAF50', fg='white', font=('TkDefaultFont', 9, 'bold'),
                                     command=self._toggle_barcodes, padx=10)
        self.barcode_btn.pack(side='left')

        # ── MAIN CONTENT (Tabs) ───────────────────────────────────────────
        style = ttk.Style()
        style.configure('TNotebook.Tab', padding=[20, 4], font=('TkDefaultFont', 10, 'bold'))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True, padx=4, pady=4)

        w = self.cfg['camera']['width']
        h = self.cfg['camera']['height']

        # ── TAB 1: PREVIEW ────────────────────────────────────────────────
        self.tab_preview = tk.Frame(self.notebook, bg='#1a1a1a')
        self.notebook.add(self.tab_preview, text="  PREVIEW  ")

        self.canvas = tk.Canvas(self.tab_preview, width=w, height=h, bg='black',
                                highlightthickness=0)
        self.canvas.pack(padx=10, pady=10)
        self._preview_image_id = None
        
        self._not_recording_overlay = self.canvas.create_text(
            w // 2, h // 2, 
            text="NOT RECORDING", 
            fill="red", 
            font=('TkDefaultFont', 32, 'bold'),
            state='normal'
        )

        # ── TAB 2: RECORD ─────────────────────────────────────────────────
        self.tab_record = tk.Frame(self.notebook, padx=12, pady=12)
        self.notebook.add(self.tab_record, text="  RECORDING  ")

        self.rec_sidebar = tk.Frame(self.tab_record, width=300)
        self.rec_sidebar.pack(side='left', fill='y', padx=(0, 15))
        
        self.rec_monitor_frame = tk.Frame(self.tab_record, bg='black', relief='sunken', borderwidth=2)
        self.rec_monitor_frame.pack(side='left', fill='both', expand=True)

        self.record_canvas = tk.Canvas(self.rec_monitor_frame, width=w, height=h, 
                                       bg='black', highlightthickness=0)
        self.record_canvas.pack(expand=True)
        self._record_preview_image_id = None

        # Sidebar Content
        config_frame = tk.LabelFrame(self.rec_sidebar, text=" Configuration ", padx=10, pady=10)
        config_frame.pack(fill='x', pady=(0, 10))

        tk.Label(config_frame, text="Animal ID:").grid(row=0, column=0, sticky='w')
        self.animal_id_var = tk.StringVar()
        self.animal_entry = tk.Entry(config_frame, textvariable=self.animal_id_var, width=20)
        self.animal_entry.grid(row=0, column=1, sticky='w', padx=(5, 0))

        tk.Label(config_frame, text="Mode:").grid(row=1, column=0, sticky='w', pady=(10, 0))
        self.mode_var = tk.StringVar(value=self.mode.value)
        self.rb_triggered = tk.Radiobutton(config_frame, text="Triggered", variable=self.mode_var,
                                           value=CaptureMode.TRIGGERED.value, command=self._on_mode_change)
        self.rb_triggered.grid(row=1, column=1, sticky='w', pady=(10, 0))
        self.rb_free = tk.Radiobutton(config_frame, text="Free Record", variable=self.mode_var,
                                      value=CaptureMode.FREE_RECORD.value, command=self._on_mode_change)
        self.rb_free.grid(row=2, column=1, sticky='w')

        fps_frame = tk.Frame(config_frame)
        fps_frame.grid(row=3, column=0, columnspan=2, sticky='w', pady=(5, 0))
        tk.Label(fps_frame, text="FPS:").pack(side='left')
        self.fps_var = tk.StringVar(value=str(self.cfg['camera']['triggered_fps']))
        fps_opts = [str(x) for x in self.cfg['gui']['free_record_fps_options']]
        self.fps_menu = ttk.Combobox(fps_frame, textvariable=self.fps_var,
                                     values=fps_opts, width=6, state='disabled')
        self.fps_menu.pack(side='left', padx=4)
        self.fps_note = tk.Label(fps_frame, text="(set on Arduino)", fg='grey', font=('TkDefaultFont', 8))
        self.fps_note.pack(side='left')

        # Triggers panel
        ctrl_frame = tk.LabelFrame(self.rec_sidebar, text=" Triggers ", padx=10, pady=10)
        ctrl_frame.pack(fill='x', pady=(0, 10))

        self.ctrl_oe_var = tk.BooleanVar(value=True)
        self.ctrl_matlab_var = tk.BooleanVar(value=True)
        self.ctrl_button_var = tk.BooleanVar(value=True)

        tk.Checkbutton(ctrl_frame, text="Monitor Open Ephys", variable=self.ctrl_oe_var).pack(anchor='w')
        tk.Checkbutton(ctrl_frame, text="Matlab Commands", variable=self.ctrl_matlab_var).pack(anchor='w')
        tk.Checkbutton(ctrl_frame, text="Hardware Button", variable=self.ctrl_button_var).pack(anchor='w')

        self.start_triggers_btn = tk.Button(ctrl_frame, text="START TRIGGERS", 
                                            bg='#2196F3', fg='white', font=('TkDefaultFont', 9, 'bold'),
                                            state='disabled', command=self._start_hardware_triggers)
        self.start_triggers_btn.pack(fill='x', pady=(10, 0))

        # Action Buttons
        btn_frame = tk.Frame(self.rec_sidebar)
        btn_frame.pack(fill='x', pady=(0, 10))

        self.arm_btn = tk.Button(btn_frame, text="ARM", width=8, height=2,
                                 bg='#FF9800', fg='white', font=('TkDefaultFont', 10, 'bold'),
                                 command=self._on_arm)
        self.arm_btn.pack(side='left', padx=2, expand=True, fill='x')

        self.record_btn = tk.Button(btn_frame, text="RECORD", width=8, height=2,
                                    bg='#4CAF50', fg='white', font=('TkDefaultFont', 10, 'bold'),
                                    command=self._on_record)
        self.record_btn.pack(side='left', padx=2, expand=True, fill='x')

        self.stop_btn = tk.Button(btn_frame, text="STOP", width=8, height=2,
                                  bg='#f44336', fg='white', font=('TkDefaultFont', 10, 'bold'),
                                  state='disabled', command=self._on_stop)
        self.stop_btn.pack(side='left', padx=2, expand=True, fill='x')

        self.oe_stop_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self.rec_sidebar, text="Stop Open Ephys on Stop", 
                       variable=self.oe_stop_var, font=('TkDefaultFont', 9)).pack(anchor='w')

        # Statistics and File
        stats_sub = tk.LabelFrame(self.rec_sidebar, text=" Statistics ", padx=10, pady=10)
        stats_sub.pack(fill='x', pady=10)

        self._stat_frame_lbl   = tk.Label(stats_sub, text="Frame:   0",   anchor='w')
        self._stat_elapsed_lbl = tk.Label(stats_sub, text="Elapsed: --",  anchor='w')
        self._stat_dropped_lbl = tk.Label(stats_sub, text="Dropped: 0",   anchor='w')
        self._stat_queue_lbl   = tk.Label(stats_sub, text="Write queue: 0", anchor='w')
        
        self._stat_frame_lbl.grid(row=0, column=0, sticky='w')
        self._stat_elapsed_lbl.grid(row=1, column=0, sticky='w')
        self._stat_dropped_lbl.grid(row=2, column=0, sticky='w')
        self._stat_queue_lbl.grid(row=3, column=0, sticky='w')

        self._file_label = tk.Label(self.rec_sidebar, text="File: --", anchor='w',
                                    fg='grey', font=('TkDefaultFont', 9),
                                    wraplength=280, justify='left')
        self._file_label.pack(fill='x', pady=(10, 0))

        self._state_label = tk.Label(self.root, text="● IDLE",
                                     font=('TkDefaultFont', 10, 'bold'), anchor='w', fg='grey', padx=10, pady=5)
        self._state_label.pack(fill='x', side='bottom')

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._on_mode_change()

    # --------------------------------------------------- tab change handler
    def _on_tab_changed(self, event):
        tab_idx = self.notebook.index(self.notebook.select())
        if tab_idx == 0:
            self.canvas.itemconfigure(self._not_recording_overlay, state='normal')
            if self.sync.arduino_connected:
                self.sync.cmd_stop_cam_free()
        else:
            self.canvas.itemconfigure(self._not_recording_overlay, state='hidden')

    # --------------------------------------------------- mode change handler
    def _on_mode_change(self):
        self.mode = CaptureMode(self.mode_var.get())
        if self.mode == CaptureMode.FREE_RECORD:
            self.fps_menu.config(state='readonly')
            self.fps_note.config(text="")
            self.fps_var.set(str(self.cfg['gui']['free_record_fps_options'][-1]))
            self.arm_btn.config(state='disabled')
            self.record_btn.config(state='normal')
            self.start_triggers_btn.config(state='disabled')
        elif self.mode == CaptureMode.TRIGGERED:
            self.fps_menu.config(state='disabled')
            self.fps_note.config(text="(set on Arduino)")
            self.fps_var.set(str(self.cfg['camera']['triggered_fps']))
            self.arm_btn.config(state='normal')
            self.record_btn.config(state='disabled')
            self.start_triggers_btn.config(state='disabled')
        else: # View Only
            self.arm_btn.config(state='disabled')
            self.record_btn.config(state='disabled')
            self.start_triggers_btn.config(state='disabled')

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
            init_cam(self._cam, self.cfg, fps, triggered, enable_output=False)
            self._update_barcode_gui()
            self._start_preview_capture()
        except Exception as e:
            messagebox.showerror("Camera Error", f"Failed to initialise camera:\n{e}")
            sys.exit(1)

    def _start_preview_capture(self):
        try:
            if self._cam.IsStreaming():
                self._cam.EndAcquisition()
        except:
            pass
        fps = self.cfg['camera']['triggered_fps']
        init_cam(self._cam, self.cfg, fps, triggered=False, enable_output=False)
        self._reset_frame_stats()
        self._stop_event.clear()
        self._ready_event.clear()
        self._cam.BeginAcquisition()
        self._capture_thread = threading.Thread(
            target=cam_capture_thread,
            args=(self._cam, None, self._preview_queue,
                  self._frame_stats, self._stop_event, self._ready_event, self.cfg, self),
            daemon=True, name="CamCaptureThread"
        )
        self._capture_thread.start()
        self._ready_event.wait(timeout=3.0)

    def _stop_capture_thread(self):
        self._stop_event.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=5.0)
        try:
            if self._cam and self._cam.IsStreaming():
                self._cam.EndAcquisition()
        except Exception:
            pass

    # --------------------------------------------------- Arduino connect
    def _connect_arduino(self):
        ok, msg, state = self.sync.connect_arduino()
        self._update_arduino_indicator(ok, msg)
        if ok:
            self._barcodes_running = (state['barcode'] == 1)
            self._update_barcode_gui()

    def _update_arduino_indicator(self, ok: bool, msg: str = ""):
        if ok:
            self._ard_dot.config(fg='#4CAF50')
            self._ard_label.config(text="Arduino: Connected")
        else:
            self._ard_dot.config(fg='#f44336')
            self._ard_label.config(text=f"Arduino: Not found")

    # --------------------------------------------------- OE callbacks
    def _oe_status_changed(self, state: str):
        colours = {'RECORD': '#f44336', 'ACQUIRE': '#FF9800', 'IDLE': '#4CAF50', 'UNREACHABLE': 'grey'}
        col = colours.get(state, 'grey')
        self._oe_dot.config(fg=col)
        self._oe_label.config(text=f"Open Ephys: {state}")

    def _oe_record_started(self):
        if not self.ctrl_oe_var.get():
            return
        if self.mode != CaptureMode.TRIGGERED:
            return
        # If we are ARMED or already recorded (due to ghost frames), START hardware!
        if not self._hardware_triggers_started:
            self._start_hardware_triggers()
        self._active_sources.add('oe')

    def _oe_record_stopped(self):
        """Open Ephys just stopped recording."""
        if not self.ctrl_oe_var.get():
            return
        if self.mode != CaptureMode.TRIGGERED:
            return
        self._trigger_stop('oe')

    # --------------------------------------------------- OR Logic trigger routing
    def _trigger_start(self, source: str):
        if not self._hardware_triggers_started:
            self._start_hardware_triggers()
        self._active_sources.add(source)

    def _trigger_stop(self, source: str):
        self._active_sources.discard(source)
        if self._active_sources:
            return
        if self.state == AppState.RECORDING:
            self.sync.cmd_stop_cam_free()
            self._end_recording()

    def _on_arduino_event(self, event: str):
        if event == 'CAM_BUTTON':
            if not self.ctrl_button_var.get():
                return
            if not self._hardware_triggers_started:
                self._start_hardware_triggers()
            elif self.state == AppState.RECORDING:
                self._trigger_stop('button')
        elif event == 'BARCODE_BUTTON':
            self._toggle_barcodes()

    def _toggle_barcodes(self):
        if self._barcodes_running:
            self.sync.cmd_stop_barcodes()
            self._barcodes_running = False
        else:
            self.sync.cmd_start_barcodes()
            self._barcodes_running = True
        self._update_barcode_gui()

    def _update_barcode_gui(self):
        if self._barcodes_running:
            self._barcode_dot.config(fg='#4CAF50')
            self._barcode_label.config(text="Barcodes: RUNNING")
            self.barcode_btn.config(text="STOP BARCODES", bg='#f44336')
        else:
            self._barcode_dot.config(fg='grey')
            self._barcode_label.config(text="Barcodes: OFF")
            self.barcode_btn.config(text="START BARCODES", bg='#4CAF50')

    # --------------------------------------------------- Arduino and Matlab callbacks
    def _matlab_started(self):
        if not self.ctrl_matlab_var.get():
            return
        if self.mode != CaptureMode.TRIGGERED:
            return
        if not self._hardware_triggers_started:
            self._start_hardware_triggers()
        self._active_sources.add('matlab')

    def _matlab_stopped(self):
        if not self.ctrl_matlab_var.get():
            return
        if self.mode != CaptureMode.TRIGGERED:
            return
        self._trigger_stop('matlab')

    # --------------------------------------------------- record / stop buttons
    def _on_arm(self):
        """Passive ARM: Prepare everything in background and wait for a trigger."""
        if self.state != AppState.IDLE:
            return

        animal = self.animal_id_var.get().strip()
        if not animal:
            messagebox.showwarning("Animal ID", "Please enter an Animal ID before recording.")
            return

        # Lock tabs immediately
        self._set_tabs_locked(True)
        self.state = AppState.ARMED
        self._hardware_triggers_started = False
        self._set_state_label("● SETTING UP CAMERA...", '#FF9800')
        
        self.arm_btn.config(state='disabled')
        self.record_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.start_triggers_btn.config(state='disabled') # Wait for setup
        
        # Start initialization in background to prevent UI freeze
        threading.Thread(target=self._async_begin_recording, daemon=True).start()

    def _async_begin_recording(self):
        """Background task to initialize camera and writer without freezing UI."""
        try:
            animal   = self.animal_id_var.get().strip()
            fps      = int(self.fps_var.get())
            filepath = self._make_filepath(animal)
            
            self._filepath    = filepath
            self._metadata    = make_metadata_stub(self.cfg, animal, self.mode, fps, filepath)
            write_metadata(filepath, self._metadata)
            
            self._stop_capture_thread()
            
            triggered = (self.mode == CaptureMode.TRIGGERED)
            init_cam(self._cam, self.cfg, fps, triggered, enable_output=True)
            
            self._write_queue = queue.Queue()
            self._reset_frame_stats()
            self._stop_event.clear()
            self._ready_event.clear()
            
            self._writer = make_writer(filepath, self.cfg, fps)
            
            # Start save thread
            self._save_thread = threading.Thread(
                target=save_thread_func, 
                args=(self._write_queue, self._writer, self._frame_stats, threading.Event()), 
                daemon=True
            )
            self._save_thread.start()
            
            self._cam.BeginAcquisition()
            
            # Start capture thread
            self._capture_thread = threading.Thread(
                target=cam_capture_thread, 
                args=(self._cam, self._write_queue, self._preview_queue, 
                      self._frame_stats, self._stop_event, self._ready_event, self.cfg, self), 
                daemon=True
            )
            self._capture_thread.start()
            
            # Wait for capture thread to signal it has flushed buffers
            if self._ready_event.wait(timeout=5.0):
                self.root.after(0, self._on_setup_complete)
            else:
                raise TimeoutError("Camera capture thread failed to start/flush")
                
        except Exception as e:
            print(f"Async Init Error: {e}")
            traceback.print_exc()
            self.root.after(0, lambda: messagebox.showerror("Init Error", f"Failed to setup recording:\n{e}"))
            self.root.after(0, self._on_stop)

    def _on_setup_complete(self):
        """Called on main thread once async setup finishes."""
        # Enable the trigger button as long as we haven't started hardware yet,
        # even if a ghost frame already pushed us to RECORDING state.
        if self.state in (AppState.ARMED, AppState.RECORDING):
            if not self._hardware_triggers_started:
                self.start_triggers_btn.config(state='normal')
            
            if self.state == AppState.ARMED:
                self._set_state_label("● ARMED — waiting for trigger...", '#FF9800')
            
            self.animal_entry.config(state='disabled')
            self._file_label.config(text=f"File: {Path(self._filepath).name}", fg='black')

    def _start_hardware_triggers(self):
        """Manually or automatically start the Arduino pulses (sends 'R')."""
        if self.state in (AppState.ARMED, AppState.RECORDING):
            print("Starting hardware triggers (R)...")
            self.sync.cmd_recording_active()
            self._hardware_triggers_started = True
            self.start_triggers_btn.config(state='disabled')
            
            # CRITICAL: If we falsely transitioned to RECORDING due to a ghost frame,
            # reset the timer NOW to the actual hardware start time.
            if self.state == AppState.RECORDING:
                self._rec_start = datetime.now()
                print("Hardware Start Verified: Resetting record timer.")

    def _on_record(self):
        """Start FREE RECORD immediately."""
        if self.state != AppState.IDLE:
            return
        # Use the same async logic for consistency
        self._on_arm() # This enters ARMED state in background
        self.root.after(100, self._check_free_record_start)

    def _check_free_record_start(self):
        """Wait for setup to finish, then fire triggers for free record."""
        if self.state == AppState.ARMED and self.start_triggers_btn['state'] == 'normal':
            self.sync.cmd_start_cam_free()
            self._start_hardware_triggers()
        elif self.state == AppState.IDLE:
            return # Cancelled
        else:
            self.root.after(100, self._check_free_record_start)

    def _on_stop(self):
        self._active_sources.clear()
        if self.state in (AppState.ARMED, AppState.RECORDING):
            # Send stop command to Arduino
            self.sync.cmd_stop_cam_free()
            
            if self.state == AppState.ARMED:
                self.state = AppState.IDLE
                self._set_state_label("● IDLE", 'grey')
                self._on_mode_change()
                self.stop_btn.config(state='disabled')
                self.start_triggers_btn.config(state='disabled')
                self._set_tabs_locked(False)
                self.animal_entry.config(state='normal')
            else:
                self._end_recording()

    def _transition_to_recording(self):
        """Called when the first frame arrives (ghost or real)."""
        if self.state == AppState.ARMED:
            self.state = AppState.RECORDING
            self._rec_start = datetime.now()
            self._set_state_label("● RECORDING", '#f44336')
            
            # If the hardware hasn't started, the user still needs the button
            if not self._hardware_triggers_started:
                self.start_triggers_btn.config(state='normal')

    def _set_tabs_locked(self, locked: bool):
        state = 'disabled' if locked else 'normal'
        for i in range(self.notebook.index('end')):
            if self.notebook.index(self.notebook.select()) == i and locked:
                continue
            self.notebook.tab(i, state=state)

    # --------------------------------------------------- recording lifecycle
    def _make_filepath(self, animal: str) -> str:
        """New pattern: {Animal} {Date}_{Time} {Rig}_{CameraLabel}.mp4"""
        now      = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H-%M-%S")
        rig_id   = self.cfg['rig'].get('filename_id', 'Rig')
        cam_lbl  = self.cfg['camera'].get('label', 'Cam1')
        
        filename = f"{animal} {date_str}_{time_str} {rig_id}_{cam_lbl}.mp4"
        
        folder   = Path(self.cfg['paths']['save_folder']) / animal
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder / filename)

    def _end_recording(self):
        self.state = AppState.FINISHING
        self._set_state_label("● FINISHING — writing to disk...", '#FF9800')
        self.stop_btn.config(state='disabled')
        threading.Thread(target=self._finish_worker, daemon=True).start()

    def _finish_worker(self):
        self._stop_capture_thread()
        if self._write_queue:
            self._write_queue.put(None)
        if self._save_thread:
            self._save_thread.join(timeout=60.0)
        if self._writer:
            try: self._writer.close()
            except: pass
            self._writer = None
        end_time = datetime.now()
        meta = finalise_metadata(self._metadata, end_time, self._frame_stats.get('captured', 0), self._frame_stats.get('dropped', 0), self._frame_stats.get('saved', 0))
        write_metadata(self._filepath, meta)
        if self.oe_stop_var.get():
            time.sleep(1.0)
            self.sync.cmd_stop_oe_recording()
        self._start_preview_capture()
        self.root.after(0, self._recording_finished)

    def _recording_finished(self):
        self._active_sources.clear()
        self.state = AppState.IDLE
        self._set_state_label("● IDLE", 'grey')
        self._on_mode_change()
        self.stop_btn.config(state='disabled')
        self.animal_entry.config(state='normal')
        self._update_barcode_gui()
        self._set_tabs_locked(False)
        saved, dropped = self._frame_stats.get('saved', 0), self._frame_stats.get('dropped', 0)
        self._file_label.config(text=f"Saved: {Path(self._filepath).name} ({saved} frames, {dropped} dropped)", fg='#4CAF50' if dropped == 0 else '#FF9800')

    # --------------------------------------------------- preview loop
    def _update_preview(self):
        try:
            frame = self._preview_queue.get_nowait()
            tab_idx = self.notebook.index(self.notebook.select())
            if tab_idx == 0 and self.state == AppState.IDLE:
                img, photo = Image.fromarray(frame), ImageTk.PhotoImage(Image.fromarray(frame))
                if self._preview_image_id is None: self._preview_image_id = self.canvas.create_image(0, 0, anchor='nw', image=photo)
                else: self.canvas.itemconfig(self._preview_image_id, image=photo)
                self.canvas._photo = photo
            elif tab_idx == 1 and self.state == AppState.RECORDING:
                img, photo = Image.fromarray(frame), ImageTk.PhotoImage(Image.fromarray(frame))
                if self._record_preview_image_id is None: self._record_preview_image_id = self.record_canvas.create_image(0, 0, anchor='nw', image=photo)
                else: self.record_canvas.itemconfig(self._record_preview_image_id, image=photo)
                self.record_canvas._photo = photo
            elif tab_idx == 1 and self.state != AppState.RECORDING:
                if self._record_preview_image_id is not None: self.record_canvas.delete(self._record_preview_image_id); self._record_preview_image_id = None
        except queue.Empty: pass
        if self.state == AppState.RECORDING:
            captured, elapsed = self._frame_stats.get('captured', 0), (datetime.now() - self._rec_start).total_seconds()
            self._stat_frame_lbl.config(text=f"Frame:   {captured:,}"); self._stat_elapsed_lbl.config(text=f"Elapsed: {elapsed:.1f} s")
        else: self._stat_frame_lbl.config(text="Frame:   0"); self._stat_elapsed_lbl.config(text="Elapsed: --")
        dropped, qsize = self._frame_stats.get('dropped', 0), (self._write_queue.qsize() if self._write_queue else 0)
        self._stat_dropped_lbl.config(text=f"Dropped: {dropped}", fg='#f44336' if dropped > 0 else 'black')
        self._stat_queue_lbl.config(text=f"Write queue: {qsize}")
        self.root.after(self.PREVIEW_INTERVAL_MS, self._update_preview)

    def _reset_frame_stats(self):
        self._frame_stats = {'captured': 0, 'dropped': 0, 'saved': 0, 'timeouts': 0, 'capture_done': False, 'error': None}

    def _set_state_label(self, text: str, colour: str):
        self._state_label.config(text=text, fg=colour)

    def _on_close(self):
        if self.state in (AppState.RECORDING, AppState.FINISHING):
            if not messagebox.askyesno("Recording in progress", "Stop and exit?"): return
            if self.state == AppState.RECORDING: self._on_stop(); self.root.after(2000, self._cleanup_and_destroy); return
        self._cleanup_and_destroy()

    def _cleanup_and_destroy(self):
        self._cleanup(); self.root.destroy()

    def _cleanup(self):
        print("_cleanup: starting...")
        try: self.sync.stop_polling(); self.sync.stop_udp_listener(); self.sync.disconnect_arduino()
        except: pass
        if self._writer:
            try: self._writer.close()
            except: pass
            self._writer = None
        self._stop_event.set()
        try:
            if self._capture_thread and self._capture_thread.is_alive(): self._capture_thread.join(timeout=3.0)
        except: pass
        try:
            if self._cam:
                try: self._cam.EndAcquisition()
                except: pass
                try: self._cam.DeInit()
                except: pass
                del self._cam; self._cam = None
            if self._cam_list: self._cam_list.Clear()
            if self._spin_system: self._spin_system.ReleaseInstance()
        except: pass
        print("_cleanup: done")

def main():
    cfg = load_config()
    root = tk.Tk()
    def handle_exception(exc_type, exc_value, exc_tb):
        msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        print(f"UNHANDLED EXCEPTION:\n{msg}"); _log_file.flush(); messagebox.showerror("Unexpected Error", msg)
        try: root.destroy()
        except: pass
    sys.excepthook = handle_exception
    try:
        app = CameraApp(root, cfg); _log_file.flush(); root.mainloop()
    except Exception:
        msg = traceback.format_exc(); print(f"CRASH:\n{msg}"); _log_file.flush()
        try: messagebox.showerror("Startup Error", msg)
        except: pass

if __name__ == '__main__':
    main()
