@echo off
title iOS Remote
cd /d %~dp0
echo Starting iOS Remote... keep this window open while you use it.
echo To stop: close this window or press Ctrl+C.
echo.
start "" /b cmd /c "timeout /t 6 >nul & start http://127.0.0.1:5000/"
python server.py %*
pause
