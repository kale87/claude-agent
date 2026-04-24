const express = require('express');
const Anthropic = require('@anthropic-ai/sdk');
const path = require('path');
const fs = require('fs');
const https = require('https');
const rateLimit = require('express-rate-limit');
const Database = require('better-sqlite3');

const app = express();
app.set('trust proxy', 1);
app.use(express.json({ limit: '20mb' }));
app.use(express.static(path.join(__dirname, 'public')));

const claude = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
const GITHUB_TOKEN = process.env.GITHUB_TOKEN || '';
const MODEL = process.env.CLAUDE_MODEL || 'claude-sonnet-4-20250514';
const MAX_TOKENS = parseInt(process.env.MAX_TOKENS, 10) || 4096;
const PORT = process.env.PORT || 3000;

// ---------------------------------------------------------------------------
// Database
// ---------------------------------------------------------------------------
const DATA_DIR = path.join(__dirname, 'data');
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
const db = new Database(path.join(DATA_DIR, 'sessions.db'));
db.exec(`
  CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New conversation',
    messages TEXT NOT NULL DEFAULT '[]',
    last_accessed INTEGER NOT NULL
  )
`);
const stmtGet    = db.prepare('SELECT * FROM sessions WHERE id = ?');
const stmtUpsert = db.prepare(`INSERT INTO sessions (id, title, messages, last_accessed) VALUES (?,?,?,?)
  ON CONFLICT(id) DO UPDATE SET title=excluded.title, messages=excluded.messages, last_accessed=excluded.last_accessed`);
const stmtDelete = db.prepare('DELETE FROM sessions WHERE last_accessed < ?');

setInterval(() => stmtDelete.run(Date.now() - 24 * 60 * 60 * 1000), 30 * 60 * 1000);

function getSession(id) {
  const row = stmtGet.get(id);
  return row ? { title: row.title, messages: JSON.parse(row.messages) } : { title: 'New conversation', messages: [] };
}
function saveSession(id, title, messages) {
  stmtUpsert.run(id, title, JSON.stringify(messages), Date.now());
}

// ---------------------------------------------------------------------------
// Agent definitions
// ---------------------------------------------------------------------------
const AGENTS = {
  manager: {
    name: 'Manager',
    emoji: '🎯',
    color: '#6366f1',
    system: `You are the Manager agent in a multi-agent system. Your job is to:
1. Understand the user's request
2. Break it into subtasks
3. Decide which specialist agents should handle each part
4. Synthesize results into a final response

Available agents:
- coder: code writing, review, debugging, GitHub operations
- researcher: research, summarizing, finding information
- writer: writing content, documentation, commit messages

When you need a specialist, output a JSON block like:
<delegate agent="coder">specific task description</delegate>

You can delegate to multiple agents. After receiving their responses, synthesize everything into a clear final answer for the user.`,
  },
  coder: {
    name: 'Coder',
    emoji: '💻',
    color: '#10b981',
    system: `You are the Coder agent. You specialize in:
- Writing clean, well-documented code
- Code review and improvement suggestions
- Debugging and fixing issues
- Explaining technical concepts
- GitHub operations (files, commits, PRs)

Be precise, technical, and always explain your reasoning. Use code blocks for all code.`,
  },
  researcher: {
    name: 'Researcher',
    emoji: '🔍',
    color: '#f59e0b',
    system: `You are the Researcher agent. You specialize in:
- Gathering and synthesizing information
- Summarizing complex topics clearly
- Finding relevant context and background
- Fact-checking and validation

Be thorough, cite your reasoning, and present findings in a structured way.`,
  },
  writer: {
    name: 'Writer',
    emoji: '✍️',
    color: '#ec4899',
    system: `You are the Writer agent. You specialize in:
- Writing clear, engaging content
- Technical documentation
- Commit messages and PR descriptions
- Editing and improving existing text

Match the tone and style appropriate for the context. Be concise but complete.`,
  },
};

// ---------------------------------------------------------------------------
// GitHub API helper
// ---------------------------------------------------------------------------
function githubRequest(method, endpoint, body = null) {
  return new Promise((resolve, reject) => {
    const options = {
      hostname: 'api.github.com',
      path: endpoint,
      method,
      headers: {
        'Authorization': `Bearer ${GITHUB_TOKEN}`,
        'User-Agent': 'claude-agent/3.0',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
      },
    };
    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          if (res.statusCode === 204) return resolve({});
          const parsed = JSON.parse(data);
          if (res.statusCode >= 400) return reject(new Error(parsed.message || `GitHub API error ${res.statusCode}`));
          resolve(parsed);
        } catch (e) { reject(e); }
      });
    });
    req.on('error', reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

// ---------------------------------------------------------------------------
// Rate limiter
// ---------------------------------------------------------------------------
const limiter = rateLimit({ windowMs: 60000, max: 60, standardHeaders: true, legacyHeaders: false });

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------
app.get('/health', (req, res) => res.json({ ok: true, model: MODEL, agents: Object.keys(AGENTS) }));

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------
app.get('/sessions', (req, res) => {
  const rows = db.prepare(`SELECT id, title, messages, last_accessed FROM sessions WHERE id LIKE 'ui-%' ORDER BY last_accessed DESC LIMIT 50`).all();
  res.json(rows.map(r => ({
    id: r.id,
    title: r.title,
    count: JSON.parse(r.messages).length,
    lastAccessed: r.last_accessed,
  })).filter(r => r.count > 0));
});

app.get('/sessions/:id/messages', (req, res) => {
  const row = stmtGet.get(req.params.id);
  res.json(row ? JSON.parse(row.messages) : []);
});

app.post('/sessions/:id/clear', (req, res) => {
  const sess = getSession(req.params.id);
  saveSession(req.params.id, 'New conversation', []);
  res.json({ ok: true });
});

// ---------------------------------------------------------------------------
// Main chat route — Manager orchestrates specialists
// ---------------------------------------------------------------------------
app.post('/chat', limiter, async (req, res) => {
  const { message, sessionId = 'default' } = req.body;
  if (!message) return res.status(400).json({ error: 'message required' });

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');

  const send = (data) => res.write(`data: ${JSON.stringify(data)}\n\n`);

  const sess = getSession(sessionId);
  const history = sess.messages;

  // Add user message
  history.push({ role: 'user', content: message, agent: 'user', ts: Date.now() });
  send({ type: 'status', agent: 'manager', status: 'thinking' });

  try {
    // Step 1: Manager plans
    const managerHistory = history
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .map(m => ({ role: m.role, content: m.content }));

    let managerResponse = '';
    const managerStream = claude.messages.stream({
      model: MODEL,
      max_tokens: MAX_TOKENS,
      system: AGENTS.manager.system,
      messages: managerHistory,
    });

    for await (const event of managerStream) {
      if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
        managerResponse += event.delta.text;
        send({ type: 'chunk', agent: 'manager', chunk: event.delta.text });
      }
    }

    // Step 2: Parse delegate tags and run specialists
    const delegateRegex = /<delegate agent="(\w+)">(.*?)<\/delegate>/gs;
    const delegations = [];
    let match;
    while ((match = delegateRegex.exec(managerResponse)) !== null) {
      delegations.push({ agent: match[1], task: match[2].trim() });
    }

    const specialistResults = {};
    for (const { agent, task } of delegations) {
      if (!AGENTS[agent]) continue;
      send({ type: 'status', agent, status: 'working' });

      let result = '';
      const stream = claude.messages.stream({
        model: MODEL,
        max_tokens: MAX_TOKENS,
        system: AGENTS[agent].system,
        messages: [{ role: 'user', content: task }],
      });

      for await (const event of stream) {
        if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
          result += event.delta.text;
          send({ type: 'chunk', agent, chunk: event.delta.text });
        }
      }

      specialistResults[agent] = result;
      send({ type: 'status', agent, status: 'done' });
    }

    // Step 3: If there were delegations, Manager synthesizes
    let finalResponse = managerResponse;
    if (delegations.length > 0) {
      send({ type: 'status', agent: 'manager', status: 'synthesizing' });
      const synthesisPrompt = `Based on these specialist results, give the user a clear final answer:\n\n${Object.entries(specialistResults).map(([a, r]) => `[${a}]: ${r}`).join('\n\n')}`;

      let synthesis = '';
      const synthStream = claude.messages.stream({
        model: MODEL,
        max_tokens: MAX_TOKENS,
        system: AGENTS.manager.system,
        messages: [...managerHistory, { role: 'assistant', content: managerResponse }, { role: 'user', content: synthesisPrompt }],
      });

      for await (const event of synthStream) {
        if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
          synthesis += event.delta.text;
          send({ type: 'synthesis_chunk', agent: 'manager', chunk: event.delta.text });
        }
      }
      finalResponse = synthesis;
    }

    // Save to session
    history.push({ role: 'assistant', content: finalResponse, agent: 'manager', ts: Date.now() });
    const title = sess.title === 'New conversation' ? message.slice(0, 60) : sess.title;
    saveSession(sessionId, title, history);

    send({ type: 'done', sessionId, delegations: delegations.map(d => d.agent) });
    Object.keys(AGENTS).forEach(a => send({ type: 'status', agent: a, status: 'idle' }));

  } catch (err) {
    console.error(err);
    send({ type: 'error', message: err.message });
  } finally {
    res.end();
  }
});

// ---------------------------------------------------------------------------
// GitHub routes
// ---------------------------------------------------------------------------
app.get('/github/user', async (req, res) => {
  try { res.json(await githubRequest('GET', '/user')); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/github/repos', async (req, res) => {
  try { res.json(await githubRequest('GET', '/user/repos?sort=updated&per_page=30')); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/github/repos/:owner/:repo/contents', async (req, res) => {
  const p = req.query.path || '';
  try { res.json(await githubRequest('GET', `/repos/${req.params.owner}/${req.params.repo}/contents/${p}`)); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/github/repos/:owner/:repo/branches', async (req, res) => {
  try { res.json(await githubRequest('GET', `/repos/${req.params.owner}/${req.params.repo}/branches`)); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/github/repos/:owner/:repo/pulls', async (req, res) => {
  try { res.json(await githubRequest('GET', `/repos/${req.params.owner}/${req.params.repo}/pulls?state=open`)); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/github/repos/:owner/:repo/commits', async (req, res) => {
  const { path: filePath, content, message, branch = 'main', sha } = req.body;
  try {
    const body = { message, content: Buffer.from(content).toString('base64'), branch };
    if (sha) body.sha = sha;
    res.json(await githubRequest('PUT', `/repos/${req.params.owner}/${req.params.repo}/contents/${filePath}`, body));
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/github/repos/:owner/:repo/pulls', async (req, res) => {
  try { res.json(await githubRequest('POST', `/repos/${req.params.owner}/${req.params.repo}/pulls`, req.body)); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/github/repos/:owner/:repo/pulls/:number/reviews', async (req, res) => {
  try { res.json(await githubRequest('POST', `/repos/${req.params.owner}/${req.params.repo}/pulls/${req.params.number}/reviews`, req.body)); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

// ---------------------------------------------------------------------------
app.listen(PORT, () => {
  console.log(`\n🚀 Claude Agent v3 running at http://localhost:${PORT}`);
  console.log(`   Model: ${MODEL}`);
  console.log(`   Agents: ${Object.keys(AGENTS).join(', ')}`);
  console.log(`   GitHub: ${GITHUB_TOKEN ? '✓ connected' : '✗ no token'}\n`);
});
