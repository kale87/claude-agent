#!/bin/bash
cd "$(dirname "$0")"
echo "Starting Claude Agent..."
docker compose up -d
if [ $? -eq 0 ]; then
    echo ""
    echo "Claude Agent is running at http://localhost:3000"
    echo ""
else
    echo ""
    echo "Failed to start. Make sure Docker Desktop is running!"
    read -p "Press Enter to close..."
fi
