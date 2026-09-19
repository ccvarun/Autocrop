@echo off
rem Three ways, because any one of them can miss.
rem
rem By name is the reliable one for a shipped client: the exe is always called
rem crop-ui.exe.  By window title catches a copy started from source, where
rem there is no exe to name.  By port catches anything still holding 8112 after
rem both, which is what actually stops the next start from working.
setlocal

echo ============================================
echo   Crop - parchi review page (stop)
echo ============================================

echo Closing Crop...
taskkill /IM crop-ui.exe /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Crop-UI*" /T /F >nul 2>&1

echo Freeing port 8112 if still in use...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8112" ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

echo Done.  Crop stopped.
timeout /t 3 >nul
exit /b 0
