const express = require('express');
const Anthropic = require('@anthropic-ai/sdk');
const path = require('path');

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

// In-memory conversation store: { sessionId: [ {role, content} ] }
const sessions = {};

// Health check
app.get('/health', (req, res) => {
  res.json({ status: 'Claude Agent is running 🚀' });
});

// Chat with memory
app.post('/chat', async (req, res) => {
  const { message, sessionId = 'default', system } = req.body;

  if (!message) {
    return res.status(400).json({ error: 'message is required' });
  }

  if (!sessions[sessionId]) {
    sessions[sessionId] = [];
  }

  sessions[sessionId].push({ role: 'user', content: message });

  try {
    const response = await client.messages.create({
      model: 'claude-sonnet-4-20250514',
      max_tokens: 1024,
      system: system || 'You are a helpful assistant.',
      messages: sessions[sessionId],
    });

    const reply = response.content[0].text;
    sessions[sessionId].push({ role: 'assistant', content: reply });

    res.json({ reply, sessionId });
  } catch (err) {
    console.error('Claude API error:', err.message);
    sessions[sessionId].pop();
    res.status(500).json({ error: err.message });
  }
});

// Clear a session
app.post('/clear', (req, res) => {
  const { sessionId = 'default' } = req.body;
  sessions[sessionId] = [];
  res.json({ cleared: true, sessionId });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Claude Agent running on http://localhost:${PORT}`);
});
