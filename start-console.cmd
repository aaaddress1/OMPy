@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-canvas.ps1" -Console %*
if errorlevel 1 pause
