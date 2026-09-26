@echo off
schtasks /end /tn WeActMonitor >nul 2>&1
schtasks /delete /f /tn WeActMonitor
pause
