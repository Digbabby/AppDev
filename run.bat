@echo off
echo ===================================================
echo      Launching Hand Tracker Testing Environment     
echo ===================================================
echo.

:: Point directly to your active tracker script file
.venv\Scripts\python.exe src\vision_module.py

echo.
echo App closed.
pause
