@echo off
title iOS Remote - setup
cd /d %~dp0
echo Installing what iOS Remote needs (one time only)...
echo.
python -m pip install --upgrade fastapi "uvicorn[standard]" websockets httpx tidevice pymobiledevice3 pillow
echo.
if errorlevel 1 (echo Something went wrong. Is Python installed? & pause & exit /b 1)
echo Done. Now double-click START.bat
pause
