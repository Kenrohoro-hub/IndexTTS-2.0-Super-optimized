@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Complete the dependency and model setup in README.md first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 -B -u server.py --port 7860
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" pause
exit /b %APP_EXIT_CODE%
