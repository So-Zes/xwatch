@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0xwatch.py" %*
) else (
  python "%~dp0xwatch.py" %*
)
if errorlevel 1 pause
