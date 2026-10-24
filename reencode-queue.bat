@echo off
rem Windows launcher: double-click or run from Command Prompt / PowerShell.
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python 3.9 or newer is required. Install it with: winget install Python.Python.3.12
  pause
  exit /b 2
)
%PY% scripts\menu.py %*
if errorlevel 1 pause
