@echo off
rem LifeGuard - double-click to start.
cd /d "%~dp0"

python -m lifeguard.portcheck 5000
if errorlevel 1 goto busy
goto run

:busy
echo.
choice /c YN /m "Stop that program and start LifeGuard"
if errorlevel 2 exit /b 1
python -m lifeguard.portcheck 5000 --kill
if errorlevel 1 (
  echo Could not free port 5000. Close the program above manually, then try again.
  pause
  exit /b 1
)

:run
echo Starting LifeGuard on http://localhost:5000  (close this window to stop)
python -m waitress --host=0.0.0.0 --port=5000 --threads=24 --call lifeguard.app:create_app
pause
