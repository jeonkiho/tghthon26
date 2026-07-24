@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto :install

where py >nul 2>nul
if not errorlevel 1 (
  py -3 -m venv .venv
  if errorlevel 1 goto :setup_error
  goto :install
)

where python >nul 2>nul
if errorlevel 1 goto :python_missing
python -m venv .venv
if errorlevel 1 goto :setup_error

:install
".venv\Scripts\python.exe" -m pip install -r requirements-api.txt
if errorlevel 1 goto :setup_error

".venv\Scripts\python.exe" run_gui.py
exit /b %errorlevel%

:python_missing
echo.
echo Python 3.10 or later was not found.
echo Install Python from https://www.python.org/downloads/ and run this file again.
pause
exit /b 1

:setup_error
echo.
echo SERAPH GUI setup failed. Check the messages above and run this file again.
pause
exit /b 1
