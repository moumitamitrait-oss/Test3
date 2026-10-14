@echo off
setlocal
title Cutover Manager - Setup and Run
cd /d "%~dp0"

echo ==============================================
echo  Cutover Manager - install dependencies and run
echo ==============================================
echo.

REM ---- 1. Check Python is available -------------------------------------
py --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo During install, tick "Add python.exe to PATH", then run this file again.
    echo.
    pause
    exit /b 1
)
py --version

REM ---- 2. Check the app files are present -------------------------------
if not exist "app.py" (
    echo [ERROR] app.py not found next to this batch file.
    pause
    exit /b 1
)
if not exist "sap_cpi_client.py" (
    echo [ERROR] sap_cpi_client.py is missing from this folder.
    echo app.py depends on it, and it was not included in CutoverManager.zip.
    echo Copy it into this folder and run this file again.
    echo.
    pause
    exit /b 1
)

REM ---- 3. Install dependencies ------------------------------------------
echo.
echo [1/3] Installing dependencies (this may take a minute the first time)...
py -m pip install --upgrade pip
py -m pip install --upgrade streamlit pandas openpyxl requests
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed. See the messages above.
    echo If you are behind a corporate proxy, ask IT for the proxy settings.
    echo.
    pause
    exit /b 1
)

REM ---- 4. Make sure at least one login exists ---------------------------
REM The prompt is kept outside any if-block on purpose: cmd expands
REM %variables% when a whole block is parsed, so a value set inside a
REM block would still look empty to a check placed in the same block.
if exist "auth_users.json" goto :start_app

echo.
echo No login user exists yet. Create one now.
set "LOGIN_NAME="
set /p "LOGIN_NAME=Enter a username for the app login: "

if "%LOGIN_NAME%"=="" (
    echo [ERROR] A username is required.
    pause
    exit /b 1
)

echo You will now be asked to enter and confirm a password.
echo.
py manage_users.py add "%LOGIN_NAME%"
if errorlevel 1 (
    echo [ERROR] Could not create the user.
    pause
    exit /b 1
)

REM ---- 5. Run the app ----------------------------------------------------
:start_app
echo.
echo [3/3] Starting Cutover Manager...
echo The app will open in your browser at http://localhost:8501
echo Leave this window open while you use the app. Press Ctrl+C here to stop it.
echo.
py -m streamlit run app.py

echo.
echo The app has stopped.
pause
endlocal