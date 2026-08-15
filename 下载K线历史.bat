@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist python_env.bat call python_env.bat
echo 正在下载 BNB/USDT 18个月 1H K线 (MEXC) ...
python scripts\download_kline_history.py --source mexc --interval 1h --months 18
echo.
echo 数据目录: data\klines\BNBUSDT\1h\chunks\
pause
