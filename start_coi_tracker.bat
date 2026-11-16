@echo off
REM One click: sets up (first time only), makes the fake samples, starts the app with its scheduler, opens the browser.
REM Dry run by default: no email leaves this computer (read them on the Outbox page).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: creating the Python environment...
  python -m venv .venv || goto :fail
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :fail
)
if not exist .env copy .env.example .env >nul
if not exist samples\bulk20 .venv\Scripts\python.exe samples\make_samples.py --bulk 20
start "" http://localhost:8010
.venv\Scripts\python.exe -m uvicorn coi.web.main:app --port 8010
goto :eof
:fail
echo Setup failed - is Python 3.11 installed and on PATH?
pause
