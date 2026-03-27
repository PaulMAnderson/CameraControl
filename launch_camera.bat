@echo off
:: Camera Capture Launcher
:: Place this .bat file in the same folder as the Python script.
:: To make a desktop shortcut: right-click -> Send To -> Desktop (create shortcut)
:: To set a custom icon on the shortcut: right-click shortcut -> Properties -> Change Icon

:: --- CONFIGURE THESE TWO LINES FOR YOUR MACHINE ---
set CONDA_ROOT=C:\ProgramData\miniforge3
:: --------------------------------------------------

cd /d "%~dp0"
start "" "%CONDA_ROOT%\pythonw.exe" "%~dp0camera_capture.py"