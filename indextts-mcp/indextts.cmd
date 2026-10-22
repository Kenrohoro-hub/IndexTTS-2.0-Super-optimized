@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "CLI_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%CLI_PY%" (
  echo [ERROR] Run run-app.cmd in this directory first. 1>&2
  exit /b 1
)
"%CLI_PY%" -B "%~dp0cli.py" %*
exit /b %errorlevel%
