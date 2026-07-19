@echo off
rem Launch the release GUI (release_gui.py) with Python 3.11.
rem Pure ASCII on purpose: CP950 consoles mis-parse non-ASCII bat lines.
setlocal
cd /d "%~dp0"
py -3.11 release_gui.py
if errorlevel 1 pause
endlocal
