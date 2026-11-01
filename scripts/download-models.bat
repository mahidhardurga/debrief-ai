@echo off
rem Downloads the speech models into desktop\models (inside the project), once. Needs internet.
cd /d "%~dp0..\desktop"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
".venv\Scripts\python.exe" backendetch_models.py
pause
