@echo off
rem Desktop demo: mock analyzer (no Claude calls, no speech models); replays a sample transcript as a live call.
cd /d "%~dp0..\desktop"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv || (echo Python 3 is required & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)
set MOCK_ANALYZER=1
set MOCK_SCREEN=1
set SESSIONS_DIR=%CD%\sessions-demo
set RECORDINGS_DIR=%CD%\sessions-demo\recordings
set PORT=5057
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:5057"
".venv\Scripts\python.exe" backend\server.py
pause
