@echo off
rem Server edition in Docker: Postgres + API + built web app on http://localhost:8000 (needs Docker Desktop running).
cd /d "%~dp0..\server"
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created server\.env - set POSTGRES_PASSWORD and SECRET_KEY in it, then run this again.
  notepad .env & pause & exit /b 0
)
docker compose up -d --build
echo Open http://localhost:8000 , sign up, then connect your Claude account on the Claude account page.
pause
