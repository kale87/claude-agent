const express = require('express');
const Anthropic = require('@anthropic-ai/sdk');
const path = require('path');
const rateLimit = require('express-rate-limit');

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

const CLAUDE_MODEL = process.env.CLAUDE_MODEL || 'claude-sonnet-4-20250514';
const MAX_TOKENS = parseInt(process.env.MAX_TOKENS, 10) || 4096;
const SESSION_TTL_MS = 60 * 60 * 1000; // 1 hour

// In-memory conversation store: { sessionId: { messages: [], lastAccessed: Date } }
const sessions = {};

// Periodically remove sessions inactive for more than SESSION_TTL_MS
setInterval(() => {
  const now = Date.now();
  for (const id of Object.keys(sessions)) {
    if (now - sessions[id].lastAccessed > SESSION_TTL_MS) {
      delete sessions[id];
    }
  }
}, 10 * 60 * 1000); // run every 10 minutes

// Rate limiter: max 30 requests per minute per IP on /chat
const chatLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 30,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'Too many requests, please slow down.' },
});

// Health check
app.get('/health', (req, res) => {
  res.json({ status: 'Claude Agent is running 🚀' });
});

// Chat with memory
app.post('/chat', chatLimiter, async (req, res) => {
  const { message, sessionId = 'default', system } = req.body;

  if (!message) {
    return res.status(400).json({ error: 'message is required' });
  }

  if (!sessions[sessionId]) {
    sessions[sessionId] = { messages: [], lastAccessed: Date.now() };
  }

  const session = sessions[sessionId];
  session.lastAccessed = Date.now();
  session.messages.push({ role: 'user', content: message });

  try {
    const response = await client.messages.create({
      model: CLAUDE_MODEL,
      max_tokens: MAX_TOKENS,
      system: system || 'You are a helpful assistant.',
      messages: session.messages,
    });

    const reply = response.content[0].text;
    session.messages.push({ role: 'assistant', content: reply });

    res.json({ reply, sessionId });
  } catch (err) {
    console.error('Claude API error:', err.message);
    session.messages.pop();
    res.status(500).json({ error: err.message });
  }
});

// Clear a session
app.post('/clear', (req, res) => {
  const { sessionId = 'default' } = req.body;
  sessions[sessionId] = { messages: [], lastAccessed: Date.now() };
  res.json({ cleared: true, sessionId });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Claude Agent running on http://localhost:${PORT}`);
  console.log(`Model: ${CLAUDE_MODEL} | Max tokens: ${MAX_TOKENS}`);
});
