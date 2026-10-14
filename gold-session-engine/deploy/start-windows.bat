@echo off
REM Start the Gold Session Engine terminal on a Windows VPS and restart it if it ever exits.
REM First time: python -m venv .venv  then  .venv\Scripts\pip install -r requirements.txt
REM To start at boot: Task Scheduler -> Create Task -> "At startup" -> run this file
REM (tick "Run whether user is logged on or not"). Turn on Autostart in the terminal.

cd /d "%~dp0\.."
:loop
".venv\Scripts\python.exe" -m gse terminal
echo Terminal exited, restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
