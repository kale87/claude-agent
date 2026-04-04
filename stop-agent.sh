#!/bin/bash
cd "$(dirname "$0")"
echo "Stopping Claude Agent..."
docker compose down
echo ""
echo "Claude Agent stopped."
