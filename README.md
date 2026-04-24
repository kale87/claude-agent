# Kal-AI

A fully local multi-agent AI system powered by **Ollama** — no API costs, no cloud.
Built with Python (FastAPI) and a pixel art office UI.

## Prerequisites

1. **Python 3.11+** — `python3 --version`
2. **Ollama** — https://ollama.ai
3. Pull a model:
```bash
ollama pull llama3.1
```

## Setup

```bash
git clone https://github.com/kale87/kal-ai
cd kal-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — add your GITHUB_TOKEN
python main.py
```

Open http://localhost:3000

## Agents

| Agent | Role |
|-------|------|
| 🎯 Manager | Understands your request, delegates to specialists, synthesizes results |
| 💻 Coder | Code writing, review, debugging, GitHub operations |
| 🔍 Researcher | Research, summarizing, context gathering |
| ✍️ Writer | Content, documentation, commit messages |

## Switching models

Edit `OLLAMA_MODEL` in `.env` and restart. Any model pulled with `ollama pull` works.
