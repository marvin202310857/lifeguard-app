@echo off
setlocal
cd /d "%~dp0"
title LifeGuard - set up automatic start

rem --- 1. find pythonw.exe (Python without a console window) -----------------------------
for /f "usebackq delims=" %%i in (`python -c "import os,sys;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"`) do set "PYW=%%i"
if not exist "%PYW%" (
  echo Could not find pythonw.exe. Make sure Python is installed and "python" works in a terminal.
  pause
  exit /b 1
)

rem --- 2. the production server must be installed ------------------------------------------
python -c "import waitress" 2>nul
if errorlevel 1 (
  echo Installing the server component ^(waitress^)...
  python -m pip install waitress
)

rem --- 3. start LifeGuard automatically every time you log in to Windows --------------------
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
(
  echo Set sh = CreateObject^("WScript.Shell"^)
  echo sh.CurrentDirectory = "%~dp0"
  echo sh.Run """%PYW%"" ""%~dp0LifeGuard.pyw""", 0, False
) > "%STARTUP%\LifeGuard.vbs"

rem --- 4. desktop shortcut that opens the dashboard ----------------------------------------
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESK=%%d"
(
  echo [InternetShortcut]
  echo URL=http://localhost:5000
) > "%DESK%\LifeGuard.url"

rem --- 5. start it now and open the dashboard ----------------------------------------------
echo.
echo Starting LifeGuard in the background...
wscript "%STARTUP%\LifeGuard.vbs"
timeout /t 12 /nobreak >nul
start "" http://localhost:5000

echo.
echo Done. LifeGuard now starts by itself whenever you log in to Windows.
echo Open it any time with the "LifeGuard" icon on your desktop.
echo Messages and errors are written to: %~dp0logs\lifeguard.log
echo.
pause
