@echo off
chcp 65001 >nul
cd /d "%~dp0"
python convert.py --pdf-parts "%~dp0pdf-parts" %*
echo.
pause
