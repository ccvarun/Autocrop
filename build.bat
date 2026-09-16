@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   building autocrop.exe and parchi.exe
echo ============================================
echo.

set "PY=python"
where py >nul 2>&1
if %errorlevel%==0 set "PY=py -3"

if not exist ".venv-build\Scripts\python.exe" (
    echo [1/6] creating build environment...
    %PY% -m venv .venv-build
    if errorlevel 1 goto :nopython
) else (
    echo [1/6] build environment already present.
)
set "VPY=.venv-build\Scripts\python.exe"

echo [2/6] installing dependencies...
"%VPY%" -m pip install --quiet --upgrade pip
"%VPY%" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :nopip

echo [3/6] running self-tests...
"%VPY%" selftest.py
if errorlevel 1 goto :testfailed
"%VPY%" parchi_selftest.py
if errorlevel 1 goto :testfailed
"%VPY%" cropui_selftest.py
if errorlevel 1 goto :testfailed

echo.
echo [4/6] building autocrop.exe...
"%VPY%" -m PyInstaller --onefile --console --clean --noconfirm ^
    --name autocrop ^
    --exclude-module tkinter --exclude-module numpy --exclude-module cv2 ^
    --exclude-module scipy --exclude-module matplotlib ^
    --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PIL.ImageQt ^
    autocrop.py
if errorlevel 1 goto :buildfailed

echo.
echo [5/6] building parchi.exe...
"%VPY%" -m PyInstaller --onefile --console --clean --noconfirm ^
    --name parchi ^
    --add-data "models;models" ^
    --exclude-module tkinter ^
    --exclude-module scipy --exclude-module matplotlib ^
    --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PIL.ImageQt ^
    parchi.py
if errorlevel 1 goto :buildfailed

echo.
echo [6/6] building crop-ui.exe...
"%VPY%" -m PyInstaller --onefile --console --clean --noconfirm ^
    --name crop-ui ^
    --add-data "web;web" ^
    --add-data "models;models" ^
    --hidden-import uvicorn.logging ^
    --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.loops.asyncio ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.protocols.http.h11_impl ^
    --hidden-import uvicorn.protocols.websockets.auto ^
    --hidden-import uvicorn.lifespan.on ^
    --exclude-module tkinter ^
    --exclude-module scipy --exclude-module matplotlib ^
    --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PIL.ImageQt ^
    cropui.py
if errorlevel 1 goto :buildfailed

rmdir /s /q build 2>nul
del autocrop.spec 2>nul
del parchi.spec 2>nul
del crop-ui.spec 2>nul

echo.
echo ============================================
echo   Done.
echo     dist\autocrop.exe   trim plain borders
echo     dist\parchi.exe     crop photographed bills
echo     dist\crop-ui.exe    the review page (START_Crop.bat runs this)
echo ============================================
goto :end

:nopython
echo.
echo Could not create the build environment.  Install Python 3.9 or newer
echo from python.org and tick "Add python.exe to PATH", then run this again.
goto :end

:nopip
echo.
echo Could not install the dependencies.  Check your internet connection.
goto :end

:testfailed
echo.
echo A self-test failed.  Not building.  Fix that first.
goto :end

:buildfailed
echo.
echo PyInstaller failed.  See the messages above.
goto :end

:end
echo.
pause
