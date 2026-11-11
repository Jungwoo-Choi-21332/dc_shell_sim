@echo off
chcp 65001 >nul
cd /d "%~dp0"
python dc_shell_sim.py %*
pause
