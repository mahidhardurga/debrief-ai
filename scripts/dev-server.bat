@echo off
rem Server edition without Docker: API + web app on http://localhost:8000 with the mock model (no Claude needed).
rem Sign up, then press Start listening: a sample call is replayed so you can click through everything.
rem For real speech and real Claude set LLM_PROVIDER=cli and ASR_ALLOW_DOWNLOAD=true before running this.
cd /d "%~dp0..\server\backend"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv || (echo Python 3.12+ is required & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt
)
if "%LLM_PROVIDER%"=="" set LLM_PROVIDER=mock
start "" /b cmd /c "timeout /t 4 >nul & start http://localhost:8000"
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8000
pause
