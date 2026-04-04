@echo off
echo Starting Claude Agent...
cd /d "%~dp0"
docker compose up -d
if %errorlevel% == 0 (
    echo.
    echo Claude Agent is running at http://localhost:3000
    echo.
) else (
    echo.
    echo Failed to start. Make sure Docker Desktop is running!
    pause
)
