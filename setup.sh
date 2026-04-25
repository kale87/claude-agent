#!/bin/bash
# Kal-AI setup script
set -e

echo "\n\U0001f680 Setting up Kal-AI...\n"

# Python venv
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -r requirements.txt --quiet

# Clone Agent Office visualizer
if [ ! -d "visualizer" ]; then
  echo "\n\U0001f3a8 Cloning Agent Office..."
  git clone https://github.com/harishkotra/agent-office.git visualizer
fi

# Install Agent Office deps
cd visualizer
npm install --silent
cd ..

echo "\n\u2705 Setup complete!"
echo "\nStart: source .venv/bin/activate && python3 main.py"
echo "Then open: http://localhost:3000"
