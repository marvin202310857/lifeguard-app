@echo off
rem Stops the background LifeGuard (it starts again at your next Windows log-in).
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*LifeGuard.pyw*' -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo LifeGuard stopped.
pause
