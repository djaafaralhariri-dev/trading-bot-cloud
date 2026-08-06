@echo off
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
python -m app.backtest --period 5y --interval 1d --walk-forward-folds 4
pause
