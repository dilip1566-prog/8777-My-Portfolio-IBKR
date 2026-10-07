@echo off
cd /d "%~dp0"
python refresh.py
if errorlevel 1 pause
