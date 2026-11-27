@echo off
rem Start the Priora UI. Closing this window stops it.
rem Uses the virtual environment made by install.bat when there is one.
setlocal
pushd "%~dp0"

rem Per-user CLI folders are not always on PATH yet (for example right after install).
if exist "%USERPROFILE%\.local\bin\claude.exe" set "PATH=%PATH%;%USERPROFILE%\.local\bin"
if exist "%APPDATA%\npm\claude.cmd" set "PATH=%PATH%;%APPDATA%\npm"
if exist "%APPDATA%\npm\codex.cmd" set "PATH=%PATH%;%APPDATA%\npm"
if exist "%LOCALAPPDATA%\agy\bin\agy.exe" set "PATH=%PATH%;%LOCALAPPDATA%\agy\bin"

set "PRIORA_PY=python"
if exist ".venv\Scripts\python.exe" set "PRIORA_PY=.venv\Scripts\python.exe"

"%PRIORA_PY%" -m patsearch.web %*
if errorlevel 1 (
  echo.
  echo [Priora] Could not start. Run install.bat first, then try again.
)
pause
popd
endlocal
