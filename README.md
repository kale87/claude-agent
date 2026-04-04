# Claude Agent 🚀

A local Claude API server running in Docker. Always available at `http://localhost:3000`.

## Setup

**1. Clone the repo**
```bash
git clone https://github.com/kale87/claude-agent.git
cd claude-agent
```

**2. Add your API key**
```bash
cp .env.example .env
```
Then open `.env` and replace `your_api_key_here` with your key from [console.anthropic.com](https://console.anthropic.com).

**3. Start the agent**
```bash
docker compose up -d
```

That's it! The agent is now running at `http://localhost:3000`.

## Endpoints

### `GET /`
Health check — confirms the agent is running.

### `POST /chat`
Send a single message to Claude.

```bash
curl -X POST http://localhost:3000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is Docker?"}'
```

Optional `system` field to set Claude's behaviour:
```json
{
  "message": "Review my code",
  "system": "You are a senior software engineer who gives concise code reviews."
}
```

### `POST /conversation`
Send a full conversation history (multi-turn).

```bash
curl -X POST http://localhost:3000/conversation \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "My name is Vladimir."},
      {"role": "assistant", "content": "Nice to meet you, Vladimir!"},
      {"role": "user", "content": "What is my name?"}
    ]
  }'
```

## Commands

```bash
# Start in background
docker compose up -d

# Stop
docker compose down

# View logs
docker compose logs -f

# Restart
docker compose restart
```

## How it works

```
You / Your apps
      │
      ▼
http://localhost:3000   ← Docker container (always on)
      │
      ▼
 Anthropic Claude API
```
