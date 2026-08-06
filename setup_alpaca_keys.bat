@echo off
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
set PYTHONPATH=%CD%
python -m app.setup_alpaca
if errorlevel 1 goto end
python -m app.test_alpaca
:end
pause
