@echo off
rem Builds the GitHub Pages edition and serves it at http://localhost:8765/
cd /d "%~dp0..\pages"
node build.mjs --serve 8765
