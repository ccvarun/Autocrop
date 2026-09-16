@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Crop - parchi review page (start)
echo ============================================

rem Prefer the built exe: a centre machine has no Python installed.
rem Beside this file first, which is how it is shipped to a client, then the
rem dist folder, which is where a local build leaves it.
if exist "%~dp0crop-ui.exe" (
    echo Starting Crop...
    start "Crop-UI" "%~dp0crop-ui.exe"
    goto :wait
)
if exist "%~dp0crop-ui\crop-ui.exe" (
    echo Starting Crop...
    start "Crop-UI" "%~dp0crop-ui\crop-ui.exe"
    goto :wait
)
if exist "dist\crop-ui.exe" (
    echo Starting Crop...
    start "Crop-UI" "dist\crop-ui.exe"
    goto :wait
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

echo Starting Crop...
start "Crop-UI" cmd /c "%PY% cropui.py"

:wait
echo Waiting for it to come up...
timeout /t 4 >nul
echo.
echo Crop is running at http://127.0.0.1:8112
echo A browser tab should have opened.  If not, open that address yourself.
echo To stop it, run STOP_Crop.bat.
echo This window can be closed.
timeout /t 4 >nul
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
