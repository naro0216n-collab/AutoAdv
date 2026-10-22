@echo off
setlocal
cd /d "%~dp0"
where pyw >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 launcher was not found. Install Python 3.11 and AutoAdv dependencies.
  echo Dependency setup: py -3.11 -m pip install -e ".[gui,audio,ocr,native]"
  pause
  exit /b 1
)
start "" pyw -3.11 "%~dp0AutoAdv.pyw"
