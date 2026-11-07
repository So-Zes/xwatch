@echo off
chcp 65001 >nul
rem スマホから xwatch を見るための設定（Tailscale の確認と tailscale serve）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0phone-setup.ps1"
pause
