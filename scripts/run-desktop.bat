@echo off
rem Debrief AI (desktop): creates desktop\.venv, installs requirements, starts the server, opens the browser.
cd /d "%~dp0..\desktop"
if not exist ".venv\Scripts\python.exe" (
  echo Creating the virtual environment...
  python -m venv .venv || (echo Python 3 is required & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo pip install failed & pause & exit /b 1)
if not exist "models\.ready" (
  echo First run: downloading the speech models into desktop\models - about 2 GB, once...
  ".venv\Scripts\python.exe" backendetch_models.py
)
if not exist ".env" copy ".env.example" ".env" >nul
set PORT_TO_OPEN=5057
for /f "tokens=1,2 delims==" %%a in ('findstr /b "PORT=" .env') do set PORT_TO_OPEN=%%b
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:%PORT_TO_OPEN%"
".venv\Scripts\python.exe" backend\server.py
pause
