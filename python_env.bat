@echo off
REM Resolve Python interpreter for launcher scripts
setlocal EnableDelayedExpansion
set "PYTHON_CMD="
set "CFG=%~dp0config.yaml"

REM 1) config.yaml -> web.python_path
if exist "%CFG%" (
    for /f "usebackq tokens=1,* delims=:" %%a in (`findstr /i /c:"python_path:" "%CFG%" 2^>nul`) do (
        set "_raw=%%b"
    )
    if defined _raw (
        set "_raw=!_raw:"=!"
        for /f "tokens=* delims= " %%p in ("!_raw!") do set "_cfg=%%p"
        if exist "!_cfg!" set "PYTHON_CMD=!_cfg!"
    )
)

if defined PYTHON_CMD goto :export

REM 2) Known install paths (newer first)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe" & goto :export
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe" & goto :export
if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python314\python.exe" & goto :export
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe" & goto :export
if exist "C:\Python312\python.exe" set "PYTHON_CMD=C:\Python312\python.exe" & goto :export
if exist "C:\Python313\python.exe" set "PYTHON_CMD=C:\Python313\python.exe" & goto :export
if exist "C:\Python314\python.exe" set "PYTHON_CMD=C:\Python314\python.exe" & goto :export
if exist "C:\Program Files\Python312\python.exe" set "PYTHON_CMD=C:\Program Files\Python312\python.exe" & goto :export
if exist "C:\Program Files\Python313\python.exe" set "PYTHON_CMD=C:\Program Files\Python313\python.exe" & goto :export
if exist "C:\Program Files\Python314\python.exe" set "PYTHON_CMD=C:\Program Files\Python314\python.exe" & goto :export

REM 3) py launcher via temp file (FOR /F + python -c breaks on parentheses)
where py >nul 2>&1
if %errorlevel% equ 0 (
    set "_pyout=%TEMP%\bnb_quant_pyexe.txt"
    del "!_pyout!" >nul 2>&1
    py -3.12 -c "import sys; open(r'!_pyout!','w').write(sys.executable)" >nul 2>&1
    if not exist "!_pyout!" py -3 -c "import sys; open(r'!_pyout!','w').write(sys.executable)" >nul 2>&1
    if exist "!_pyout!" (
        set /p PYTHON_CMD=<"!_pyout!"
        del "!_pyout!" >nul 2>&1
        if exist "!PYTHON_CMD!" goto :export
        set "PYTHON_CMD="
    )
)

REM 4) PATH fallback (may be Baota Python 3.8)
where python >nul 2>&1
if %errorlevel% equ 0 set "PYTHON_CMD=python"

:export
if not defined PYTHON_CMD (
    endlocal & set "PYTHON_CMD="
    exit /b 1
)
for %%O in ("!PYTHON_CMD!") do endlocal & set "PYTHON_CMD=%%~O"
exit /b 0