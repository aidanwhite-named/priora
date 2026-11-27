@echo off
rem Priora uninstaller: removes everything this program created
rem (search records, cache, settings, keys, .venv, desktop shortcut) and the
rem LLM CLI sessions that were created from this folder.
rem The work is done by tools\uninstall.ps1; this file only starts it.
rem Options:  /dryrun  (only show what would be removed)   /y  (do not ask)   /keepkeys  (keep .env)
setlocal
set "PRIORA_ROOT=%~dp0"
set "PRIORA_ARGS=%*"

if not exist "%PRIORA_ROOT%tools\uninstall.ps1" (
  echo [Priora] tools\uninstall.ps1 was not found next to this file.
  if not defined PRIORA_NOPAUSE pause
  exit /b 1
)
where powershell >nul 2>nul
if errorlevel 1 (
  echo [Priora] Windows PowerShell was not found.
  if not defined PRIORA_NOPAUSE pause
  exit /b 1
)

rem Run from outside the folder so nothing in it is held open by this window.
cd /d "%SystemRoot%"
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$s=[IO.File]::ReadAllText((Join-Path $env:PRIORA_ROOT 'tools\uninstall.ps1'),[Text.Encoding]::UTF8); & ([scriptblock]::Create($s))"
set "PRIORA_RC=%ERRORLEVEL%"

echo.
if not defined PRIORA_NOPAUSE pause
exit /b %PRIORA_RC%
