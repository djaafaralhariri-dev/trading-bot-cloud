@echo off
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
set PYTHONPATH=%CD%
python -m app.test_alpaca
pause
