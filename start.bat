@echo off
REM db-checker - start the local web UI and open it.
REM Pick a server in the page, click Run checks. Reachable from this machine only.
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Install Python 3.9 or newer, ticking "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist "servers.json" (
  copy "servers.json.example" "servers.json" >nul
  echo No servers.json found - created one from servers.json.example.
  echo.
  echo Add your servers and passwords, save, then run this again.
  notepad servers.json
  exit /b 1
)

echo Starting db-checker UI...
echo Your browser will open at http://127.0.0.1:8787
echo Close this window to stop the server.
echo.
python app.py
pause
