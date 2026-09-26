@echo off
setlocal EnableExtensions

rem === WeAct monitor: scheduled task installer (auto-elevates) ===

>nul 2>&1 net session || (
  echo Requesting administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "SCRIPT=%~dp0weact_monitor.py"
if not exist "%SCRIPT%" (
  echo [!] weact_monitor.py not found next to this installer.
  pause
  exit /b 1
)

set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW for /f "delims=" %%i in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\pythonw.exe" 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW for /f "delims=" %%i in ('dir /b /s "C:\Python\pythonw.exe" 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
  echo [!] pythonw.exe not found. Install Python or add it to PATH.
  pause
  exit /b 1
)
echo pythonw: %PYW%

python -c "import serial, PIL, psutil" >nul 2>&1
if errorlevel 1 (
  echo Installing missing Python packages: pyserial pillow psutil ...
  python -m pip install --quiet pyserial pillow psutil
)

schtasks /create /f /tn WeActMonitor /sc onlogon /rl highest /tr "\"%PYW%\" \"%SCRIPT%\""
if errorlevel 1 (
  echo [!] Task creation failed. Try running this file as administrator.
  pause
  exit /b 1
)
schtasks /run /tn WeActMonitor
echo.
echo Done: task "WeActMonitor" created (autostart at logon, admin rights) and started.
pause
