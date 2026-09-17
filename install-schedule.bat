@echo off
REM db-checker - register the automatic weekly run (Windows Task Scheduler).
REM
REM Runs run-scheduled.bat every Monday at 08:00 and writes reports into reports\.
REM Nothing is emailed and nothing is applied - the report is just there when you
REM come in. Run this once; uninstall-schedule.bat removes it.
setlocal
cd /d "%~dp0"

set TASKNAME=DbChecker-Weekly

if not exist ".env" (
  echo Run check-once.bat first, so .env exists and the checks are known to work.
  pause
  exit /b 1
)

REM The action is a single .bat, deliberately. schtasks /TR rejects compound
REM commands ("&&"), and a folder path containing a space breaks the quoting.
schtasks /Create /TN "%TASKNAME%" /TR "\"%~dp0run-scheduled.bat\"" /SC WEEKLY /D MON /ST 08:00 /RL LIMITED /F
if errorlevel 1 (
  echo.
  echo FAILED - the scheduled task was NOT created. See the error above.
  pause
  exit /b 1
)

REM Prove it exists rather than trusting the exit code.
schtasks /Query /TN "%TASKNAME%" >nul 2>&1
if errorlevel 1 (
  echo.
  echo FAILED - schtasks reported success but the task is not there.
  pause
  exit /b 1
)

echo.
echo Created "%TASKNAME%" - every Monday 08:00.
echo   Report: %~dp0reports\latest.html
echo   JSON:   %~dp0reports\latest.json
echo   Log:    %~dp0reports\schedule.log
echo.
echo Testing it now...
schtasks /Run /TN "%TASKNAME%"
echo.
echo Give it a minute, then check that reports\latest.html has today's time on it.
echo Remove the schedule later with uninstall-schedule.bat
echo.
pause
