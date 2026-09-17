@echo off
REM Local Scribe launcher for Windows.
REM Finds a supported Python, then hands over to bootstrap.py, which creates
REM the virtual environment, installs dependencies, stops any running
REM instance, and starts the server.

setlocal
cd /d "%~dp0"
title Local Scribe

REM Prefer the Python launcher with an explicit supported version, because the
REM default "python" on this machine may be a version without wheels for the
REM transcription engine.
for %%V in (3.13 3.12 3.11 3.10) do (
  py -%%V -c "import sys" >nul 2>&1 && (
    py -%%V bootstrap.py %*
    goto done
  )
)

REM Fall back to the plain launcher or python on PATH; bootstrap.py checks the
REM version itself and will locate a supported interpreter if it can.
py -c "import sys" >nul 2>&1 && (
  py bootstrap.py %*
  goto done
)

python -c "import sys" >nul 2>&1 && (
  python bootstrap.py %*
  goto done
)

REM No Python anywhere on this machine: bootstrap.py's own download logic
REM needs an interpreter to run it, so fetch one directly with PowerShell
REM first. Same self-contained CPython build (python-build-standalone, what
REM `uv` installs) that bootstrap.py downloads; cached under .pyruntime\,
REM shared with bootstrap.py's own copy of this logic.
echo   No Python found. Trying to download one automatically...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\fetch_python.ps1" > "%TEMP%\localscribe_fetch_python.txt"
set /p FETCHED_PYTHON=<"%TEMP%\localscribe_fetch_python.txt"
del "%TEMP%\localscribe_fetch_python.txt" >nul 2>&1

if not "%FETCHED_PYTHON%"=="" if exist "%FETCHED_PYTHON%" (
  "%FETCHED_PYTHON%" bootstrap.py %*
  goto done
)

echo.
echo   Local Scribe could not start: no Python interpreter was found, and one
echo   could not be downloaded automatically (no internet connection?).
echo.
echo   Install Python 3.13 from https://www.python.org/downloads/
echo   During installation, tick "Add python.exe to PATH".
echo.
pause
exit /b 1

:done
set STATUS=%ERRORLEVEL%
if not "%STATUS%"=="0" (
  echo.
  echo   Local Scribe exited with status %STATUS%.
  pause
)
exit /b %STATUS%
