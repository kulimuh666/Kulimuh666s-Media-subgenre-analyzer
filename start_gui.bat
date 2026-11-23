@echo off
title Kulimuh666's Media Subgenre Analyzer V.0035
cd /d "%~dp0"

echo ========================================
echo  Kulimuh666's Media Subgenre Analyzer V.0035
echo ========================================
echo.

REM Alten Server auf Port 8765 beenden, falls noch einer laeuft
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765" ^| findstr "ABHOREN LISTENING"') do (
    echo Stoppe alten Server PID %%P
    taskkill /PID %%P /F >nul 2>&1
)

echo Starte Server...
where py >nul 2>&1
if %errorlevel%==0 (
  start "VDJ Server" cmd /k py -3 server.py
) else (
  start "VDJ Server" cmd /k python server.py
)

echo Warte auf Server...
timeout /t 3 >nul

echo Oeffne GUI...
start "" "%~dp0gui.html"

echo.
echo Fertig. Server-Fenster nicht schliessen.
pause
