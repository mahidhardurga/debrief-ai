@echo off
rem Server edition: lint + backend tests + a syntax check of the shared UI (same as CI).
cd /d "%~dp0..\server\backend"
if not exist ".venv\Scripts\python.exe" (python -m venv .venv & ".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt)
".venv\Scripts\python.exe" -m ruff check app tests || goto :fail
".venv\Scripts\python.exe" -m pytest -q || goto :fail
where node >nul 2>nul && (node --check "%~dp0..\shared\ui\app.js" && node --check "%~dp0..\server\frontend\platform.js" && node --check "%~dp0..\desktop\frontend\platform.js" || goto :fail)
echo ALL PASSED & pause & exit /b 0
:fail
echo FAILED & pause & exit /b 1
