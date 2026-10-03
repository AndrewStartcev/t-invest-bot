@echo off
setlocal
cd /d "%~dp0"
title T-Invest Bot - local admin

set "TINVEST_PYTHON="
if defined T_INVEST_PYTHON if exist "%T_INVEST_PYTHON%" set "TINVEST_PYTHON=%T_INVEST_PYTHON%"
if not defined TINVEST_PYTHON for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-*") do if exist "%%~fD\python.exe" set "TINVEST_PYTHON=%%~fD\python.exe"
if not defined TINVEST_PYTHON for /d %%D in ("%ProgramFiles%\Python*") do if exist "%%~fD\python.exe" set "TINVEST_PYTHON=%%~fD\python.exe"

if not defined TINVEST_PYTHON (
    echo Python was not found. Set T_INVEST_PYTHON to the full path of python.exe.
    pause
    exit /b 1
)

echo T-Invest Bot: http://127.0.0.1:8765/
echo Keep this console open while monitoring. Press Ctrl+C to stop.
"%TINVEST_PYTHON%" -u demo_admin.py
if errorlevel 1 (
    echo Admin stopped with an error. Check the message above.
    pause
)
