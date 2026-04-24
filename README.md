# Multi-Agent Local AI — Claude Agent v3

A multi-agent AI system powered by **Ollama** (fully local, no API costs) with a pixel art office UI and GitHub integration.

## Prerequisites

1. Install Ollama: https://ollama.ai
2. Pull a model:
```bash
ollama pull llama3.1
```

## Setup

```bash
git clone https://github.com/kale87/claude-agent
cd claude-agent
npm install
cp .env.example .env
# Edit .env — add your GITHUB_TOKEN
npm start
```

Open http://localhost:3000

## Agents

| Agent | Role |
|-------|------|
| 🎯 Manager | Understands your request, delegates to specialists, synthesizes results |
| 💻 Coder | Code writing, review, debugging, GitHub operations |
| 🔍 Researcher | Research, summarizing, context gathering |
| ✍️ Writer | Content, documentation, commit messages |

## GitHub Integration

Set `GITHUB_TOKEN` in `.env` (needs `repo` scope). Then use the GitHub panel in the UI to browse repos, commit files, create branches and PRs.

## Switching models

Edit `OLLAMA_MODEL` in `.env` and restart. Any model you've pulled with `ollama pull` works.
