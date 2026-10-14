@echo off
rem Windows launcher: creates a private environment on the first run, then opens the wizard.
cd /d "%~dp0"
if not exist .venv (
  echo First start: installing the needed packages ...
  py -3 -m venv .venv || goto :error
  .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)
.venv\Scripts\python run_wizard.py %*
goto :eof
:error
echo.
echo Setup failed. Check that Python 3.9 or newer is installed ("py -3 --version") and that pip can reach the package index.
pause
