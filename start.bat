@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 xwatch.py %*
) else (
  python xwatch.py %*
)
if errorlevel 1 pause
