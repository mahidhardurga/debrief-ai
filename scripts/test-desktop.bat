@echo off
rem Desktop edition tests (mock analyzer, scratch folders): API + files, role-based topics, telling speakers apart, pasted AI tokens.
cd /d "%~dp0..\desktop"
if not exist ".venv\Scripts\python.exe" (python -m venv .venv & ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt)
".venv\Scripts\python.exe" tests\backend_smoke.py || goto :fail
".venv\Scripts\python.exe" tests\dynamic_rubric_test.py || goto :fail
".venv\Scripts\python.exe" tests\speakers_test.py || goto :fail
".venv\Scripts\python.exe" tests\ai_tokens_test.py || goto :fail
echo ALL PASSED & pause & exit /b 0
:fail
echo FAILED & pause & exit /b 1
