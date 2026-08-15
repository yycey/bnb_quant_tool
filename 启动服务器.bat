@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"

echo ============================================================
echo    策略云行 · 量化平台策略管理系统
echo    trading engine: analysis + open + SL/TP
echo ============================================================
echo.
echo  Web: 宝塔站点打开控制台
echo  本窗口 = 交易进程（无桌面 GUI）
echo.
call "%~dp0python_env.bat"
if not defined PYTHON_CMD (
    echo [ERROR] Python NOT found. Set web.python_path in config.yaml
    pause
    exit /b 1
)

if not exist "%~dp0data\logs" mkdir "%~dp0data\logs"
if not exist "%~dp0data\locks" mkdir "%~dp0data\locks"

echo Using: %PYTHON_CMD%
echo API: config.yaml
echo Log: data\logs\autopilot.log
echo Close this window to stop trading.
echo.

"%PYTHON_CMD%" "%~dp0scripts\ensure_pyarrow.py"
if errorlevel 1 (
    echo [WARN] pyarrow check/install failed — K线将回退 csv
)

"%PYTHON_CMD%" "%~dp0autopilot_daemon.py"
pause
