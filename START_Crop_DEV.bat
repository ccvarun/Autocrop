@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Crop - running from source, not the exe
echo ============================================
echo.
echo Use this while the page is being changed.  It serves web\index.html
echo straight off the disk, so a refresh in the browser shows every edit
echo without rebuilding crop-ui.exe.  STOP_Crop.bat stops it either way.
echo.

if not exist ".venv-build\Scripts\python.exe" (
    echo The build environment is missing.  Run build.bat once first.
    pause
    exit /b 1
)

echo Starting Crop from source...
start "Crop-UI" cmd /c ".venv-build\Scripts\python.exe cropui.py"

timeout /t 4 >nul
echo.
echo Crop is running at http://127.0.0.1:8112
echo A browser tab should have opened.  If not, open that address yourself.
echo To stop it, run STOP_Crop.bat.
echo This window can be closed.
timeout /t 4 >nul
exit /b 0
