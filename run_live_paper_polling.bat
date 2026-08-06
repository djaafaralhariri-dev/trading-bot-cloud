@echo off
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
set PYTHONPATH=%CD%
python -m app.live_paper --source polling
pause
