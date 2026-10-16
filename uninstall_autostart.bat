@echo off
rem Turns automatic start off and stops the background LifeGuard.
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\LifeGuard.vbs" 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*LifeGuard.pyw*' -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Automatic start is off and LifeGuard is stopped.
echo (The "LifeGuard" desktop icon is still there; delete it if you like.)
pause
