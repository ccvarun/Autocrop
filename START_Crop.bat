@echo off
rem One window, not two.
rem
rem This used to launch the exe with START, which opened a second console and
rem left this one printing "this window can be closed" while the other said
rem "leave this window open".  Two black windows with opposite instructions is
rem how "the main console is getting closed" was reported as a fault.
rem
rem Now the exe runs in THIS window.  It is the only one, closing it stops
rem Crop, and that is what a person would expect closing it to do.
setlocal
cd /d "%~dp0"
title Crop-UI

rem Prefer the built exe: a centre machine has no Python installed.
rem Beside this file first, which is how it is shipped to a client, then the
rem dist folder, which is where a local build leaves it.
if exist "%~dp0crop-ui.exe" (
    "%~dp0crop-ui.exe"
    goto :done
)
if exist "%~dp0crop-ui\crop-ui.exe" (
    "%~dp0crop-ui\crop-ui.exe"
    goto :done
)
if exist "dist\crop-ui.exe" (
    "dist\crop-ui.exe"
    goto :done
)

rem Otherwise run from source, using the build environment if it exists.
set "PY=python"
where py >nul 2>&1
if %errorlevel%==0 set "PY=py -3"
if exist ".venv-build\Scripts\python.exe" set "PY=.venv-build\Scripts\python.exe"

%PY% -c "import fastapi, uvicorn, cv2" >nul 2>&1
if errorlevel 1 (
    echo First run: installing what Crop needs ^(one-time, ~1 min^)...
    if not exist ".venv-build\Scripts\python.exe" (
        %PY% -m venv .venv-build
        if errorlevel 1 goto :nopython
    )
    set "PY=.venv-build\Scripts\python.exe"
    ".venv-build\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv-build\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    if errorlevel 1 goto :nopip
)

%PY% cropui.py

:done
exit /b 0

:nopython
echo.
echo Python was not found.  Install Python 3.9 or newer from python.org,
echo tick "Add python.exe to PATH", then run this again.
pause
exit /b 1

:nopip
echo.
echo Could not install what Crop needs.  Check the internet connection.
pause
exit /b 1
