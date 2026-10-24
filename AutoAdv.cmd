@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 launcher was not found. Install Python 3.11 first.
  pause
  exit /b 1
)
py -3.11 "%~dp0scripts\prepare_windows_runtime.py" --repo "%~dp0" --launch
if errorlevel 1 (
  echo AutoAdv runtime setup failed. See the message above.
  pause
  exit /b 1
)
