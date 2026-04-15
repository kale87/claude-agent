const express = require('express');
const Anthropic = require('@anthropic-ai/sdk');
const path = require('path');
const fs = require('fs');
const rateLimit = require('express-rate-limit');
const Database = require('better-sqlite3');

const app = express();
app.set('trust proxy', 1);
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

const CLAUDE_MODEL    = process.env.CLAUDE_MODEL  || 'claude-sonnet-4-20250514';
const MAX_TOKENS      = parseInt(process.env.MAX_TOKENS, 10) || 4096;
const SESSION_TTL_MS  = 60 * 60 * 1000;          // 1 hour
const MAX_CTX_CHARS   = 80_000;                   // ~20k tokens, conservative

// ---------------------------------------------------------------------------
// SQLite setup
// ---------------------------------------------------------------------------
const DATA_DIR = path.join(__dirname, 'data');
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

const db = new Database(path.join(DATA_DIR, 'sessions.db'));
db.exec(`
  CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    messages      TEXT NOT NULL DEFAULT '[]',
    last_accessed INTEGER NOT NULL
  )
`);

const stmtGet    = db.prepare('SELECT * FROM sessions WHERE id = ?');
const stmtUpsert = db.prepare(`
  INSERT INTO sessions (id, messages, last_accessed) VALUES (?, ?, ?)
  ON CONFLICT(id) DO UPDATE SET messages = excluded.messages, last_accessed = excluded.last_accessed
`);
const stmtClear  = db.prepare('UPDATE sessions SET messages = ?, last_accessed = ? WHERE id = ?');
const stmtDelete = db.prepare('DELETE FROM sessions WHERE last_accessed < ?');

function getSession(sessionId) {
  const row = stmtGet.get(sessionId);
  return row ? JSON.parse(row.messages) : [];
}

function saveSession(sessionId, messages) {
  stmtUpsert.run(sessionId, JSON.stringify(messages), Date.now());
}

// Periodically purge sessions idle for more than SESSION_TTL_MS
setInterval(() => {
  const deleted = stmtDelete.run(Date.now() - SESSION_TTL_MS);
  if (deleted.changes > 0) console.log(`Purged ${deleted.changes} expired session(s)`);
}, 10 * 60 * 1000);

// ---------------------------------------------------------------------------
// Context window management
// ---------------------------------------------------------------------------
function trimMessages(messages) {
  // Remove oldest pairs (user+assistant) until total chars are within limit.
  // Always keep at least the last user message.
  let msgs = [...messages];
  while (msgs.length > 1) {
    const total = msgs.reduce((sum, m) => sum + (typeof m.content === 'string' ? m.content.length : 0), 0);
    if (total <= MAX_CTX_CHARS) break;
    // Drop the oldest two messages (one user + one assistant turn)
    msgs.splice(0, 2);
    console.log('Context trimmed: removed oldest turn to stay within limit');
  }
  return msgs;
}

// ---------------------------------------------------------------------------
// Rate limiter
// ---------------------------------------------------------------------------
const chatLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 30,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'Too many requests, please slow down.' },
});

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------
app.get('/health', (req, res) => {
  res.json({ status: 'Claude Agent is running 🚀' });
});

// Blocking chat (backward-compatible)
app.post('/chat', chatLimiter, async (req, res) => {
  const { message, sessionId = 'default', system } = req.body;
  if (!message) return res.status(400).json({ error: 'message is required' });

  const messages = getSession(sessionId);
  messages.push({ role: 'user', content: message });
  const trimmed = trimMessages(messages);

  try {
    const response = await client.messages.create({
      model: CLAUDE_MODEL,
      max_tokens: MAX_TOKENS,
      system: system || 'You are a helpful assistant.',
      messages: trimmed,
    });

    const reply = response.content?.[0]?.text;
    if (!reply) return res.status(502).json({ error: 'Unexpected response from Claude API' });

    trimmed.push({ role: 'assistant', content: reply });
    saveSession(sessionId, trimmed);
    res.json({ reply, sessionId });
  } catch (err) {
    console.error('Claude API error:', err.message);
    const status = err.status ?? 500;
    res.status(status).json({ error: err.message });
  }
});

// Streaming chat via Server-Sent Events
app.post('/chat/stream', chatLimiter, async (req, res) => {
  const { message, sessionId = 'default', system } = req.body;
  if (!message) return res.status(400).json({ error: 'message is required' });

  const messages = getSession(sessionId);
  messages.push({ role: 'user', content: message });
  const trimmed = trimMessages(messages);

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');

  let fullReply = '';
  try {
    const stream = client.messages.stream({
      model: CLAUDE_MODEL,
      max_tokens: MAX_TOKENS,
      system: system || 'You are a helpful assistant.',
      messages: trimmed,
    });

    for await (const event of stream) {
      if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
        const chunk = event.delta.text;
        fullReply += chunk;
        res.write(`data: ${JSON.stringify({ chunk })}\n\n`);
      }
    }

    if (fullReply) {
      trimmed.push({ role: 'assistant', content: fullReply });
      saveSession(sessionId, trimmed);
    }
    res.write(`data: ${JSON.stringify({ done: true, sessionId })}\n\n`);
  } catch (err) {
    console.error('Claude stream error:', err.message);
    res.write(`data: ${JSON.stringify({ error: err.message })}\n\n`);
  } finally {
    res.end();
  }
});

// Clear a session
app.post('/clear', (req, res) => {
  const { sessionId = 'default' } = req.body;
  stmtClear.run('[]', Date.now(), sessionId);
  // if row didn't exist yet, upsert an empty one
  stmtUpsert.run(sessionId, '[]', Date.now());
  res.json({ cleared: true, sessionId });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Claude Agent running on http://localhost:${PORT}`);
  console.log(`Model: ${CLAUDE_MODEL} | Max tokens: ${MAX_TOKENS}`);
});
