@echo off
setlocal
cd /d "%~dp0"
python menu.py
if errorlevel 1 pause
