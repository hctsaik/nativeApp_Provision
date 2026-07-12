@echo off
setlocal
cd /d "%~dp0"
py -3.11 provision_gui.py
if errorlevel 1 pause

