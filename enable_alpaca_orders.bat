@echo off
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
set PYTHONPATH=%CD%
echo ACHTUNG: Nur Alpaca PAPER. Kein echtes Geld.
python -m app.setup_alpaca --enable-orders
pause
