@echo off
chcp 65001 >nul
rem xwatch のセットアップ（自動起動の登録、デスクトップのショートカット、初回の起動）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
pause
