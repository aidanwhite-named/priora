@echo off
rem Priora installer. Safe to run again (acts as repair / dependency update).
rem The work is done by tools\install.ps1; this file only starts it.
rem Options:  /y  (no questions, use defaults)   /cli:claude,codex,agy  or  /cli:none   /noshortcut
setlocal
set "PRIORA_ROOT=%~dp0"
set "PRIORA_ARGS=%*"

if not exist "%PRIORA_ROOT%tools\install.ps1" (
  echo [Priora] tools\install.ps1 was not found next to this file.
  echo          If you opened this from inside a zip, extract ALL files first,
  echo          then run install.bat from the extracted folder.
  if not defined PRIORA_NOPAUSE pause
  exit /b 1
)
where powershell >nul 2>nul
if errorlevel 1 (
  echo [Priora] Windows PowerShell was not found. It is required for installation.
  if not defined PRIORA_NOPAUSE pause
  exit /b 1
)

rem The script is read as UTF-8 text and run from memory, so neither the PowerShell
rem execution policy nor the "downloaded from the internet" mark can block it.
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$s=[IO.File]::ReadAllText((Join-Path $env:PRIORA_ROOT 'tools\install.ps1'),[Text.Encoding]::UTF8); & ([scriptblock]::Create($s))"
set "PRIORA_RC=%ERRORLEVEL%"

echo.
if not defined PRIORA_NOPAUSE pause
exit /b %PRIORA_RC%
