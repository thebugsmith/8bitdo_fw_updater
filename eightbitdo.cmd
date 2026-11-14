@echo off
setlocal
set "EIGHTBITDO_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%EIGHTBITDO_PY%" (
  echo Environment missing. From this folder, run: py -3 install.py
  exit /b 1
)
"%EIGHTBITDO_PY%" "%~dp0eightbitdo.py" %*
exit /b %ERRORLEVEL%
