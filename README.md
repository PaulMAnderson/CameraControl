# Unified Sync Controller

The **Unified Sync Controller** is a centralized orchestration system designed for high-speed camera capture synchronized with external experimental triggers. It is specifically built for neurophysiology experiments requiring precise temporal alignment between video data, electrophysiology (Open Ephys), and behavioral tasks (Matlab).

## Features

- **Multi-Source Synchronization**: Synchronizes camera capture using an "OR Logic" trigger strategy across:
  - **Matlab**: Low-latency UDP triggers (Port 5005).
  - **Open Ephys**: HTTP-based status polling and control.
  - **Hardware**: Physical button triggers via Arduino.
- **Hardware-Timed Precision**: Utilizes an Arduino as a slave pulse generator for sub-millisecond TTL accuracy (Camera pulses and Barcode timestamps).
- **Flexible Operation Modes**:
  - **Synced Record**: 400Hz hardware-triggered capture, armed for external start/stop.
  - **Free Record**: Software-timed capture at user-defined framerates.
  - **View Only**: 15fps preview without disk I/O.
- **Fail-Safe Metadata**: Immediate JSON sidecar generation on recording start with frame-accurate statistics updated on exit.
- **Optimized Video Encoding**: Supports hardware-accelerated codecs (Intel QSV, NVIDIA NVENC, AMD AMF) for real-time HEVC encoding.

## Components

- **`camera_capture.py`**: The main Python application and Tkinter-based GUI.
- **`sync_controller.py`**: Multi-threaded I/O layer for Serial, UDP, and HTTP communication.
- **`barcode_sync_millis_button/`**: Arduino firmware for TTL pulse generation and barcode timing.
- **`docs/`**: Detailed design and implementation documentation.

## Documentation

Comprehensive design plans and implementation details can be found in the [docs/](docs/) directory:
- **[Design Plans](docs/design-plans/)**: Architectural overview of the Unified Sync Controller.
- **[Implementation Plans](docs/implementation-plans/)**: Phased development roadmap.
- **[Test Plans](docs/test-plans/)**: Verification strategies and requirements.

## Future Roadmap

The ongoing development is tracked in [TODO.md](TODO.md). Key upcoming features include:
- Robust Arduino firmware hardening.
- Unified I/O layer for Serial, UDP, and HTTP.
- Advanced "OR Logic" for multi-source camera triggering.
- Fail-safe metadata logging.

## Setup

1. **Hardware**: Connect an Arduino (see `barcode_sync_millis_button.ino` for pinouts) and a compatible camera.
2. **Environment**: Install Python 3.x and required dependencies (Pypylon for Basler cameras, FFmpeg for encoding).
3. **Configuration**: 
   - Copy `config.json.example` to `config.json`.
   - Edit `config.json` with your rig-specific paths, IP addresses, and hardware ports.
4. **Execution**: Run `python camera_capture.py` to start the controller.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
