@echo off
REM Registers the scheduled task without changing the execution policy.
REM
REM   scripts\schedule-dry-run.cmd 20:20
setlocal
if "%~1"=="" (
    echo Usage: scripts\schedule-dry-run.cmd HH:MM
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0schedule-dry-run.ps1" -At "%~1"
endlocal
