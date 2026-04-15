const express = require('express');
const Anthropic = require('@anthropic-ai/sdk');
const path = require('path');
const fs = require('fs');
const rateLimit = require('express-rate-limit');
const Database = require('better-sqlite3');

const app = express();
app.set('trust proxy', 1);
app.use(express.json({ limit: '20mb' }));
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
function contentLen(m) {
  if (typeof m.content === 'string') return m.content.length;
  if (Array.isArray(m.content)) return m.content.reduce((s, b) => s + (b.text?.length || 0), 0);
  return 0;
}

function trimMessages(messages) {
  let msgs = [...messages];
  while (msgs.length > 1) {
    const total = msgs.reduce((sum, m) => sum + contentLen(m), 0);
    if (total <= MAX_CTX_CHARS) break;
    msgs.splice(0, 2);
    console.log('Context trimmed: removed oldest turn to stay within limit');
  }
  return msgs;
}

// Strip base64 image data before writing to SQLite
function serializeForDb(messages) {
  return messages.map(m => {
    if (!Array.isArray(m.content)) return m;
    return { ...m, content: m.content.map(b => b.type === 'image' ? { type: 'text', text: '[uploaded image]' } : b) };
  });
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
  const { message, sessionId = 'default', system, images } = req.body;
  if (!message) return res.status(400).json({ error: 'message is required' });

  const messages = getSession(sessionId);
  const userContent = (images?.length)
    ? [...images.map(img => ({ type: 'image', source: { type: 'base64', media_type: img.mediaType, data: img.data } })), { type: 'text', text: message }]
    : message;
  messages.push({ role: 'user', content: userContent });
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
    saveSession(sessionId, serializeForDb(trimmed));
    res.json({ reply, sessionId });
  } catch (err) {
    console.error('Claude API error:', err.message);
    const status = err.status ?? 500;
    res.status(status).json({ error: err.message });
  }
});

// Streaming chat via Server-Sent Events
app.post('/chat/stream', chatLimiter, async (req, res) => {
  const { message, sessionId = 'default', system, images } = req.body;
  if (!message) return res.status(400).json({ error: 'message is required' });

  const messages = getSession(sessionId);
  const userContent = (images?.length)
    ? [...images.map(img => ({ type: 'image', source: { type: 'base64', media_type: img.mediaType, data: img.data } })), { type: 'text', text: message }]
    : message;
  messages.push({ role: 'user', content: userContent });
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
      saveSession(sessionId, serializeForDb(trimmed));
    }
    res.write(`data: ${JSON.stringify({ done: true, sessionId })}\n\n`);
  } catch (err) {
    console.error('Claude stream error:', err.message);
    res.write(`data: ${JSON.stringify({ error: err.message })}\n\n`);
  } finally {
    res.end();
  }
});

// List recent sessions
app.get('/sessions', (req, res) => {
  const rows = db.prepare(`
    SELECT id, messages, last_accessed FROM sessions
    WHERE id LIKE 'ui-%'
    ORDER BY last_accessed DESC LIMIT 50
  `).all();
  res.json(rows.map(r => {
    const msgs = JSON.parse(r.messages);
    // Find the first user message with meaningful text (not just an image placeholder)
    let preview = '';
    let imageOnly = false;
    for (const m of msgs) {
      if (m.role !== 'user') continue;
      let text = '';
      if (typeof m.content === 'string') {
        text = m.content;
      } else if (Array.isArray(m.content)) {
        text = m.content
          .filter(b => b.type === 'text' && b.text !== '[uploaded image]')
          .map(b => b.text).join(' ');
        if (!text.trim()) imageOnly = true;
      }
      text = text.trim();
      if (text && text !== '(see attached images)') { preview = text.slice(0, 80); break; }
    }
    if (!preview) preview = imageOnly ? '\uD83D\uDCF7 Image conversation' : '';
    return { id: r.id, lastAccessed: r.last_accessed, preview, count: msgs.length };
  }).filter(s => s.count > 0));
});

// Get messages for a session
app.get('/sessions/:id/messages', (req, res) => {
  const row = stmtGet.get(req.params.id);
  if (!row) return res.json([]);
  res.json(JSON.parse(row.messages));
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
