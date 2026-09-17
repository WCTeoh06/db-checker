@echo off
REM db-checker - what the scheduled task runs. Not meant to be double-clicked.
REM
REM This exists as its own file because schtasks /TR cannot take a compound
REM command: "&&" is rejected outright, and a path containing a space (like
REM "New folder") breaks the quoting even when it is not. One .bat, one action.
setlocal
cd /d "%~dp0"

if not exist "reports" mkdir "reports"

REM Text and HTML reports, plus reports\latest.html, written by the script itself.
REM --no-baseline: this run writes the reports but does NOT become the new
REM "since last run" baseline. The weekly agent owns that, because it is the
REM thing that reads the diff and files a bug about it. If this run consumed
REM the diff first, the agent would open every Monday to "no change".
python db_checker.py --no-baseline >> "reports\schedule.log" 2>&1

REM JSON alongside, for anything that wants to read the findings programmatically.
python db_checker.py --json --no-save > "reports\latest.json" 2>>"reports\schedule.log"

echo [%DATE% %TIME%] run finished, exit %ERRORLEVEL% >> "reports\schedule.log"
