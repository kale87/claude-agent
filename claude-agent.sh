#!/bin/bash
cd "$(dirname "$0")"

if docker compose ps --services --filter "status=running" | grep -q "claude-agent"; then
    echo "Stopping Claude Agent..."
    docker compose down
    echo ""
    echo "Claude Agent stopped."
else
    echo "Starting Claude Agent..."
    docker compose up -d
    echo ""
    echo "Claude Agent is running at http://localhost:3000"
fi
