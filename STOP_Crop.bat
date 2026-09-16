@echo off
setlocal

echo ============================================
echo   Crop - parchi review page (stop)
echo ============================================

echo Closing the Crop window...
taskkill /FI "WINDOWTITLE eq Crop-UI*" /T /F >nul 2>&1

echo Freeing port 8112 if still in use...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8112" ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

echo Done.  Crop stopped.
timeout /t 3 >nul
exit /b 0
