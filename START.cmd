@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\launch.py %*
  goto result
)
python -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
if not errorlevel 1 (
  python scripts\launch.py %*
  goto result
)
py -3 -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
if not errorlevel 1 (
  py -3 scripts\launch.py %*
  goto result
)
echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/
pause
exit /b 1
:result
if errorlevel 1 pause
