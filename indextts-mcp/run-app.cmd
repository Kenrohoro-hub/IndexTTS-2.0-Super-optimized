@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "BASE_PY=%~dp0..\.venv\Scripts\python.exe"
set "MCP_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%BASE_PY%" (
  echo [ERROR] Complete the setup in the project root README.md first. 1>&2
  goto fail
)
if not exist "%MCP_PY%" (
  "%BASE_PY%" -m venv "%~dp0.venv" 1>&2
  if errorlevel 1 goto fail
)
"%MCP_PY%" -c "from importlib.metadata import version; import win32com.client; assert version('mcp')=='2.3.0' and version('requests')=='2.32.5'" >nul 2>&1
if errorlevel 1 (
  "%MCP_PY%" -m pip install --disable-pip-version-check -r "%~dp0requirements.lock" 1>&2
  if errorlevel 1 goto fail
)
"%MCP_PY%" -m pip check 1>&2
if errorlevel 1 goto fail
"%MCP_PY%" -B "%~dp0mcp_server.py" --print-config > "%~dp0client-config.json.tmp"
if errorlevel 1 goto fail
move /y "%~dp0client-config.json.tmp" "%~dp0client-config.json" >nul
if errorlevel 1 goto fail
echo [READY] Copy the config below into your MCP client:
type "%~dp0client-config.json"
echo.
echo See README.md for usage. This window is setup only.
if /i not "%~1"=="--no-pause" pause
exit /b 0
:fail
echo [ERROR] Setup failed. Read the error above. 1>&2
if /i not "%~1"=="--no-pause" pause
exit /b 1
