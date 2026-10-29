@echo off
rem start.bat — double-click entry point for the field laptop.
rem The real work is in start_all.ps1 (PowerShell 5.1, the Windows default); the batch
rem file only exists so an operator can start the system from Explorer without opening
rem a terminal. It pauses on failure so the error message does not vanish with the window.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1"
if errorlevel 1 pause