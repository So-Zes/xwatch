@echo off
chcp 65001 >nul
rem このフォルダの xwatch を止めます（AI 用の --mcp で動いているものは止めません）
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Join-Path '%~dp0' 'xwatch.py'; $n = 0; Get-CimInstance Win32_Process -Filter \"Name like 'py%%.exe'\" | Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($p.ToLower()) -and -not $_.CommandLine.Contains('--mcp') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $n++ }; if ($n) { Write-Host 'xwatch を止めました。' } else { Write-Host '動いている xwatch はありませんでした。' }"
pause
