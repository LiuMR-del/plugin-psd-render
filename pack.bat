@echo off
chcp 65001 >nul
rem PSD 批量生图 — Windows 一键打包源码 ZIP
rem 输出: dist\plugin-psd-render.zip
cd /d "%~dp0"

set "OUT_DIR=dist"
set "OUT_FILE=%OUT_DIR%\plugin-psd-render.zip"

if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"
if exist "%OUT_FILE%" del /f /q "%OUT_FILE%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$root=(Get-Location).Path;" ^
  "$out=Join-Path $root '%OUT_FILE%';" ^
  "$exclude=@('\.git\','\.venv\','\__pycache__\','\.pycache\','\build\','\dist\','\node_modules\','\自检结果\','\.ruff_cache\','\.zcode\');" ^
  "$files=Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object {" ^
  "  $full=$_.FullName;" ^
  "  -not ($exclude | Where-Object { $full.Contains($_) }) -and" ^
  "  $_.Name -ne '.DS_Store' -and" ^
  "  $_.Name -ne '.env' -and" ^
  "  -not $_.Name.StartsWith('.env.') -and" ^
  "  $_.Extension -ne '.pyc' -and" ^
  "  $_.Extension -ne '.spec'" ^
  "};" ^
  "Compress-Archive -LiteralPath $files.FullName -DestinationPath $out -Force;" ^
  "Write-Host ('打包完成: ' + $out)"

if errorlevel 1 pause
