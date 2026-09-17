@echo off
REM db-checker - check ONE server and write report files. No UI, no dropdown.
REM
REM This is the automation path: it uses the single connection in .env, and is what
REM the scheduled task runs. To pick a server from a list, use start.bat instead.
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Install Python 3.9 or newer, ticking "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo No .env file found - created one from .env.example.
  echo.
  echo Open .env and fill in:
  echo   PGPASSWORD    the password for the database user
  echo   PGHOST/PGUSER/PGDATABASE  if they differ from the defaults
  echo   APP_LOGS_DIR  path to MyApp's database\logs folder
  echo.
  echo Then run this again.
  notepad .env
  exit /b 1
)

echo Running checks...
echo.
python db_checker.py
if errorlevel 2 goto :fail

REM Open the report in the default browser.
if exist "reports\latest.html" (
  start "" "reports\latest.html"
) else (
  echo No report file was produced - see the output above.
)

goto :eof

:fail
echo.
echo Could not run - see the message above.
echo Most often: PGPASSWORD missing from .env, or psql not found ^(set PSQL_PATH^).
pause
exit /b 1
