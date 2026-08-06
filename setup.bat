@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements.txt
if not exist ".env" copy ".env.example" ".env" >nul
if not exist "logs" mkdir logs
if not exist "scanner_output" mkdir scanner_output
echo.
echo Setup abgeschlossen.
pause
