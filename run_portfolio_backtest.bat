@echo off
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
python -m app.portfolio_backtest --period 5y --interval 1d
pause
