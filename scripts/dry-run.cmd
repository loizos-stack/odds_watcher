@echo off
REM Runs dry-run.ps1 without requiring a change to the machine's execution
REM policy: .cmd files are not subject to it, and -ExecutionPolicy Bypass
REM applies only to this one invocation.
REM
REM   scripts\dry-run.cmd              start now
REM   scripts\dry-run.cmd 20:20        start at 20:20 local time
setlocal
set "AT=%~1"
if "%AT%"=="" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dry-run.ps1"
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dry-run.ps1" -At "%AT%"
)
endlocal
