const express = require('express');
const Anthropic = require('@anthropic-ai/sdk');
const path = require('path');
const fs = require('fs');
const { execFile } = require('child_process');
const { promisify } = require('util');
const rateLimit = require('express-rate-limit');
const Database = require('better-sqlite3');

const execFileAsync = promisify(execFile);
const app = express();
app.set('trust proxy', 1);
app.use(express.json({ limit: '20mb' }));
app.use(express.static(path.join(__dirname, 'public')));

const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

const CLAUDE_MODEL   = process.env.CLAUDE_MODEL  || 'claude-sonnet-4-20250514';
const MAX_TOKENS     = parseInt(process.env.MAX_TOKENS, 10) || 4096;
const SESSION_TTL_MS = 60 * 60 * 1000;
const MAX_CTX_CHARS  = 80_000;

const GIT_WORKDIR = process.env.GIT_WORKDIR || '/workspace';

const GIT_BIN = (() => {
  const candidates = ['/usr/bin/git', '/usr/local/bin/git', '/bin/git'];
  for (const p of candidates) {
    try { fs.accessSync(p, fs.constants.X_OK); return p; } catch (_) {}
  }
  return 'git';
})();
console.log(`Git binary: ${GIT_BIN}`);

const DATA_DIR = path.join(__dirname, 'data');
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

const db = new Database(path.join(DATA_DIR, 'sessions.db'));
db.exec(`
  CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    messages      TEXT NOT NULL DEFAULT '[]',
    system_prompt TEXT NOT NULL DEFAULT '',
    last_accessed INTEGER NOT NULL
  )
`);
try { db.exec('ALTER TABLE sessions ADD COLUMN system_prompt TEXT NOT NULL DEFAULT \'\''); } catch (_) {}

const stmtGet    = db.prepare('SELECT * FROM sessions WHERE id = ?');
const stmtUpsert = db.prepare(`
  INSERT INTO sessions (id, messages, system_prompt, last_accessed) VALUES (?, ?, ?, ?)
  ON CONFLICT(id) DO UPDATE SET messages = excluded.messages, system_prompt = excluded.system_prompt, last_accessed = excluded.last_accessed
`);
const stmtClear  = db.prepare('UPDATE sessions SET messages = ?, last_accessed = ? WHERE id = ?');
const stmtDelete = db.prepare('DELETE FROM sessions WHERE last_accessed < ?');

function getSession(sessionId) {
  const row = stmtGet.get(sessionId);
  return row ? { messages: JSON.parse(row.messages), systemPrompt: row.system_prompt || '' } : { messages: [], systemPrompt: '' };
}
function saveSession(sessionId, messages, systemPrompt = '') {
  stmtUpsert.run(sessionId, JSON.stringify(messages), systemPrompt, Date.now());
}
setInterval(() => {
  const deleted = stmtDelete.run(Date.now() - SESSION_TTL_MS);
  if (deleted.changes > 0) console.log(`Purged ${deleted.changes} expired session(s)`);
}, 10 * 60 * 1000);

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
  }
  return msgs;
}
function serializeForDb(messages) {
  return messages.map(m => {
    if (!Array.isArray(m.content)) return m;
    return { ...m, content: m.content.map(b => b.type === 'image' ? { type: 'text', text: '[uploaded image]' } : b) };
  });
}

const chatLimiter = rateLimit({
  windowMs: 60 * 1000, max: 30,
  standardHeaders: true, legacyHeaders: false,
  message: { error: 'Too many requests, please slow down.' },
});

app.get('/health', (req, res) => {
  res.json({ status: 'Claude Agent is running \uD83D\uDE80', model: CLAUDE_MODEL });
});

app.post('/chat', chatLimiter, async (req, res) => {
  const { message, sessionId = 'default', system, images } = req.body;
  if (!message) return res.status(400).json({ error: 'message is required' });
  const sess = getSession(sessionId);
  const messages = sess.messages;
  const systemPrompt = system || sess.systemPrompt || 'You are a helpful assistant.';
  const userContent = (images?.length)
    ? [...images.map(img => ({ type: 'image', source: { type: 'base64', media_type: img.mediaType, data: img.data } })), { type: 'text', text: message }]
    : message;
  messages.push({ role: 'user', content: userContent });
  const trimmed = trimMessages(messages);
  try {
    const response = await client.messages.create({ model: CLAUDE_MODEL, max_tokens: MAX_TOKENS, system: systemPrompt, messages: trimmed });
    const reply = response.content?.[0]?.text;
    if (!reply) return res.status(502).json({ error: 'Unexpected response from Claude API' });
    trimmed.push({ role: 'assistant', content: reply });
    saveSession(sessionId, serializeForDb(trimmed), systemPrompt);
    res.json({ reply, sessionId, usage: response.usage });
  } catch (err) {
    console.error('Claude API error:', err.message);
    res.status(err.status ?? 500).json({ error: err.message });
  }
});

app.post('/chat/stream', chatLimiter, async (req, res) => {
  const { message, sessionId = 'default', system, images } = req.body;
  if (!message) return res.status(400).json({ error: 'message is required' });
  const sess = getSession(sessionId);
  const messages = sess.messages;
  const systemPrompt = system || sess.systemPrompt || 'You are a helpful assistant.';
  const userContent = (images?.length)
    ? [...images.map(img => ({ type: 'image', source: { type: 'base64', media_type: img.mediaType, data: img.data } })), { type: 'text', text: message }]
    : message;
  messages.push({ role: 'user', content: userContent });
  const trimmed = trimMessages(messages);
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  let fullReply = '', usage = null;
  try {
    const stream = client.messages.stream({ model: CLAUDE_MODEL, max_tokens: MAX_TOKENS, system: systemPrompt, messages: trimmed });
    for await (const event of stream) {
      if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
        const chunk = event.delta.text;
        fullReply += chunk;
        res.write(`data: ${JSON.stringify({ chunk })}\n\n`);
      }
      if (event.type === 'message_delta' && event.usage) usage = event.usage;
    }
    if (fullReply) {
      trimmed.push({ role: 'assistant', content: fullReply });
      saveSession(sessionId, serializeForDb(trimmed), systemPrompt);
    }
    res.write(`data: ${JSON.stringify({ done: true, sessionId, usage })}\n\n`);
  } catch (err) {
    console.error('Claude stream error:', err.message);
    res.write(`data: ${JSON.stringify({ error: err.message })}\n\n`);
  } finally {
    res.end();
  }
});

app.post('/sessions/:id/system', (req, res) => {
  const { systemPrompt = '' } = req.body;
  const sess = getSession(req.params.id);
  saveSession(req.params.id, sess.messages, systemPrompt);
  res.json({ ok: true });
});

app.get('/sessions', (req, res) => {
  const rows = db.prepare(`SELECT id, messages, system_prompt, last_accessed FROM sessions WHERE id LIKE 'ui-%' ORDER BY last_accessed DESC LIMIT 50`).all();
  res.json(rows.map(r => {
    const msgs = JSON.parse(r.messages);
    let preview = '', imageOnly = false;
    for (const m of msgs) {
      if (m.role !== 'user') continue;
      let text = '';
      if (typeof m.content === 'string') { text = m.content; }
      else if (Array.isArray(m.content)) {
        text = m.content.filter(b => b.type === 'text' && b.text !== '[uploaded image]').map(b => b.text).join(' ');
        if (!text.trim()) imageOnly = true;
      }
      text = text.trim();
      if (text && text !== '(see attached images)') { preview = text.slice(0, 80); break; }
    }
    if (!preview) preview = imageOnly ? '\uD83D\uDCF7 Image conversation' : '';
    return { id: r.id, lastAccessed: r.last_accessed, preview, count: msgs.length, systemPrompt: r.system_prompt || '' };
  }).filter(s => s.count > 0));
});

app.get('/sessions/:id/messages', (req, res) => {
  const row = stmtGet.get(req.params.id);
  if (!row) return res.json([]);
  res.json(JSON.parse(row.messages));
});

app.post('/clear', (req, res) => {
  const { sessionId = 'default' } = req.body;
  stmtClear.run('[]', Date.now(), sessionId);
  stmtUpsert.run(sessionId, '[]', '', Date.now());
  res.json({ cleared: true, sessionId });
});

app.get('/sessions/:id/export', (req, res) => {
  const row = stmtGet.get(req.params.id);
  if (!row) return res.status(404).json({ error: 'Session not found' });
  const msgs = JSON.parse(row.messages);
  const lines = [`# Chat Export\n_Session: ${req.params.id}_\n`];
  msgs.forEach(m => {
    const role = m.role === 'user' ? '**You**' : '**Claude**';
    let text = typeof m.content === 'string' ? m.content : (m.content.find(b => b.type === 'text')?.text || '');
    lines.push(`### ${role}\n${text}\n`);
  });
  res.setHeader('Content-Type', 'text/markdown');
  res.setHeader('Content-Disposition', `attachment; filename="chat-${req.params.id}.md"`);
  res.send(lines.join('\n'));
});

// ---------------------------------------------------------------------------
// Git routes — use full binary path, inherit process.env without overriding PATH
// ---------------------------------------------------------------------------
async function git(...args) {
  const { stdout, stderr } = await execFileAsync(GIT_BIN, args, {
    cwd: GIT_WORKDIR,
    env: { ...process.env, GIT_TERMINAL_PROMPT: '0' },
    maxBuffer: 4 * 1024 * 1024,
  });
  return { stdout: stdout.trim(), stderr: stderr.trim() };
}

app.get('/git/status', async (req, res) => {
  try {
    const [statusRes, branchRes, logRes] = await Promise.all([
      git('status', '--porcelain=v1'),
      git('rev-parse', '--abbrev-ref', 'HEAD'),
      git('log', '--oneline', '-10'),
    ]);
    const files = statusRes.stdout.split('\n').filter(Boolean).map(line => ({ status: line.slice(0, 2).trim(), path: line.slice(3) }));
    const commits = logRes.stdout.split('\n').filter(Boolean).map(line => ({ sha: line.slice(0, 7), message: line.slice(8) }));
    res.json({ branch: branchRes.stdout, files, commits, workdir: GIT_WORKDIR });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.get('/git/branches', async (req, res) => {
  try {
    const [localRes, currentRes] = await Promise.all([
      git('branch', '--format=%(refname:short)'),
      git('rev-parse', '--abbrev-ref', 'HEAD'),
    ]);
    res.json({ branches: localRes.stdout.split('\n').filter(Boolean), current: currentRes.stdout });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.post('/git/checkout', async (req, res) => {
  const { branch } = req.body;
  if (!branch) return res.status(400).json({ error: 'branch is required' });
  try { await git('checkout', branch); res.json({ ok: true, branch }); }
  catch (err) { res.status(500).json({ error: err.stderr || err.message }); }
});

app.post('/git/branch', async (req, res) => {
  const { name } = req.body;
  if (!name) return res.status(400).json({ error: 'name is required' });
  try { await git('checkout', '-b', name); res.json({ ok: true, branch: name }); }
  catch (err) { res.status(500).json({ error: err.stderr || err.message }); }
});

app.get('/git/diff', async (req, res) => {
  try {
    const args = ['diff'];
    if (req.query.path) args.push('--', req.query.path);
    const result = await git(...args);
    res.json({ diff: result.stdout });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.post('/git/stage', async (req, res) => {
  const { paths = ['.'] } = req.body;
  try { await git('add', '--', ...paths); res.json({ ok: true, staged: paths }); }
  catch (err) { res.status(500).json({ error: err.stderr || err.message }); }
});

app.post('/git/unstage', async (req, res) => {
  const { paths = ['.'] } = req.body;
  try { await git('restore', '--staged', '--', ...paths); res.json({ ok: true }); }
  catch (err) { res.status(500).json({ error: err.stderr || err.message }); }
});

app.post('/git/commit', async (req, res) => {
  const { message, stageAll = false } = req.body;
  if (!message) return res.status(400).json({ error: 'message is required' });
  try {
    if (stageAll) await git('add', '-A');
    const result = await git('commit', '-m', message);
    res.json({ ok: true, output: result.stdout });
  } catch (err) {
    const msg = err.stderr || err.message || '';
    if (msg.includes('nothing to commit')) return res.json({ ok: false, output: 'Nothing to commit.' });
    res.status(500).json({ error: msg });
  }
});

app.post('/git/push', async (req, res) => {
  const { remote = 'origin', branch, setUpstream = false } = req.body;
  try {
    const currentBranch = branch || (await git('rev-parse', '--abbrev-ref', 'HEAD')).stdout;
    const args = ['push'];
    if (setUpstream) args.push('--set-upstream');
    args.push(remote, currentBranch);
    const result = await git(...args);
    res.json({ ok: true, output: result.stdout || result.stderr });
  } catch (err) { res.status(500).json({ error: err.stderr || err.message }); }
});

app.post('/git/pull', async (req, res) => {
  try {
    const result = await git('pull');
    res.json({ ok: true, output: result.stdout || result.stderr });
  } catch (err) { res.status(500).json({ error: err.stderr || err.message }); }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Claude Agent running on http://localhost:${PORT}`);
  console.log(`Model: ${CLAUDE_MODEL} | Max tokens: ${MAX_TOKENS}`);
  console.log(`Git binary: ${GIT_BIN} | Workdir: ${GIT_WORKDIR}`);
});
