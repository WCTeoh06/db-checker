@echo off
REM db-checker - remove the automatic weekly run.
setlocal
set TASKNAME=DbChecker-Weekly

schtasks /Delete /TN "%TASKNAME%" /F
if errorlevel 1 (
  echo Task not found, or already removed.
) else (
  echo Removed "%TASKNAME%".
)
echo.
pause
