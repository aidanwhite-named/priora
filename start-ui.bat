@echo off
rem Start the prior-art search UI. Closing this window stops it.
cd /d "%~dp0"
python -m patsearch.web %*
pause
