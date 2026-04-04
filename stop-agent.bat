@echo off
echo Stopping Claude Agent...
cd /d "%~dp0"
docker compose down
echo.
echo Claude Agent stopped.
pause
