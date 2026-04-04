@echo off
cd /d "%~dp0"

docker compose ps --services --filter "status=running" | findstr "claude-agent" >nul 2>&1

if %errorlevel% == 0 (
    echo Stopping Claude Agent...
    docker compose down
    echo.
    echo Claude Agent stopped.
) else (
    echo Starting Claude Agent...
    docker compose up -d
    echo.
    echo Claude Agent is running at http://localhost:3000
)

timeout /t 3 >nul
