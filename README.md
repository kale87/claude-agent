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

## Configuration

All configuration is via environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required)* | Your Anthropic API key |
| `PORT` | `3000` | Port the server listens on |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Claude model to use |
| `MAX_TOKENS` | `4096` | Maximum tokens per response |

## Endpoints

### `GET /health`
Returns the server status.

```bash
curl http://localhost:3000/health
# {"status":"Claude Agent is running 🚀"}
```

### `POST /chat`
Send a message to Claude. Conversation history is kept in memory per `sessionId` and automatically expires after 1 hour of inactivity.

```bash
curl -X POST http://localhost:3000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is Docker?"}'
```

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `message` | string | yes | The user message |
| `sessionId` | string | no | Conversation ID for multi-turn memory (default: `"default"`) |
| `system` | string | no | System prompt to set Claude's behaviour |

Example with all fields:
```json
{
  "message": "Review my code",
  "sessionId": "my-project",
  "system": "You are a senior software engineer who gives concise code reviews."
}
```

**Response:**
```json
{
  "reply": "Here's my review...",
  "sessionId": "my-project"
}
```

### `POST /clear`
Clears the conversation history for a session.

```bash
curl -X POST http://localhost:3000/clear \
  -H "Content-Type: application/json" \
  -d '{"sessionId": "my-project"}'
# {"cleared": true, "sessionId": "my-project"}
```

## Rate Limiting

The `/chat` endpoint is rate limited to **30 requests per minute per IP**. Exceeding the limit returns a `429` response:

```json
{"error": "Too many requests, please slow down."}
```

Each response includes standard rate limit headers:

```
RateLimit-Limit: 30
RateLimit-Remaining: 28
RateLimit-Reset: 45
RateLimit-Policy: 30;w=60
```

## Error Handling

| Status | Meaning |
|---|---|
| `400` | `message` field missing from request body |
| `429` | Rate limit exceeded (30 req/min) or Claude API rate limit hit |
| `500` | Unexpected server error |
| `502` | Claude API returned an unexpected response |

## Commands

```bash
# Start in background
docker compose up -d

# Rebuild after code changes
docker compose up -d --build

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
