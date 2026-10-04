@echo off
setlocal
cd /d "%~dp0"
title T-Invest Bot - setup

set "TINVEST_PYTHON="
if defined T_INVEST_PYTHON if exist "%T_INVEST_PYTHON%" set "TINVEST_PYTHON=%T_INVEST_PYTHON%"
if not defined TINVEST_PYTHON for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-*") do if exist "%%~fD\python.exe" set "TINVEST_PYTHON=%%~fD\python.exe"
if not defined TINVEST_PYTHON for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do if exist "%%~fD\python.exe" set "TINVEST_PYTHON=%%~fD\python.exe"
if not defined TINVEST_PYTHON for /d %%D in ("%ProgramFiles%\Python*") do if exist "%%~fD\python.exe" set "TINVEST_PYTHON=%%~fD\python.exe"
if not defined TINVEST_PYTHON (
    echo Python 3.10+ was not found. Install Python, then run install.cmd again.
    pause
    exit /b 1
)

"%TINVEST_PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo Python 3.10 or newer is required.
    pause
    exit /b 1
)
"%TINVEST_PYTHON%" -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Setup complete. Run start_admin.cmd to open the admin panel.
pause
exit /b 0

:failed
echo Setup failed. Check the message above and run install.cmd again.
pause
exit /b 1
