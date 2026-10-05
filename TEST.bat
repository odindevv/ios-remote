@echo off
title iOS Remote - self-test
cd /d %~dp0
python tests\selftest.py
pause
