@echo off
chcp 65001 >nul
cd /d "%~dp0"

where php >nul 2>&1
if errorlevel 1 (
  echo [错误] 未找到 php，请安装 PHP 7.4+ 并加入 PATH
  pause
  exit /b 1
)

echo 启动 PHP 内置服务器: http://127.0.0.1:8787
echo 按 Ctrl+C 停止
php -S 127.0.0.1:8787 -t public public/router.php 2>nul
if errorlevel 1 (
  php -S 127.0.0.1:8787 -t public
)
