@echo off
rem Double-click this file to run the media scraper.
rem First run creates .venv, installs the dependencies, and downloads Chromium.
setlocal enableextensions
cd /d "%~dp0"

set "PY=py -3"
%PY% --version >nul 2>nul
if errorlevel 1 set "PY=python"
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python was not found on this PC.
  echo Install it from https://www.python.org/downloads/ and tick
  echo "Add python.exe to PATH" during setup, then run this file again.
  echo.
  pause
  exit /b 1
)

if exist ".venv\Scripts\python.exe" goto run

echo.
echo First run: creating the virtual environment and installing dependencies...
echo (this takes a few minutes and only happens once)
echo.
%PY% -m venv .venv
if errorlevel 1 goto setup_failed
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto setup_failed
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto setup_failed

:run
echo.
echo Starting the scraper. Paste the page URL when prompted.
echo.
".venv\Scripts\python.exe" media_url_scraper.py %*
echo.
pause
exit /b 0

:setup_failed
echo.
echo Setup failed. Check your internet connection and run this file again.
echo.
pause
exit /b 1
