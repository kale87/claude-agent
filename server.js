const express = require('express');
const http = require('http');
const path = require('path');
const fs = require('fs');
const https = require('https');
const rateLimit = require('express-rate-limit');
const Database = require('better-sqlite3');

const app = express();
app.set('trust proxy', 1);
app.use(express.json({ limit: '20mb' }));
app.use(express.static(path.join(__dirname, 'public')));

const OLLAMA_HOST  = process.env.OLLAMA_HOST  || 'http://localhost:11434';
const OLLAMA_MODEL = process.env.OLLAMA_MODEL || 'llama3.1';
const GITHUB_TOKEN = process.env.GITHUB_TOKEN || '';
const PORT         = parseInt(process.env.PORT, 10) || 3000;

// ---------------------------------------------------------------------------
// Ollama streaming helper
// ---------------------------------------------------------------------------
function ollamaStream(system, messages, onChunk) {
  return new Promise((resolve, reject) => {
    const ollamaMessages = [
      { role: 'system', content: system },
      ...messages,
    ];
    const body = JSON.stringify({
      model: OLLAMA_MODEL,
      messages: ollamaMessages,
      stream: true,
    });
    const url = new URL('/api/chat', OLLAMA_HOST);
    const isHttps = url.protocol === 'https:';
    const lib = isHttps ? https : http;
    const req = lib.request(
      {
        hostname: url.hostname,
        port: url.port || (isHttps ? 443 : 80),
        path: url.pathname,
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      },
      (res) => {
        let full = '';
        res.on('data', (chunk) => {
          const lines = chunk.toString().split('\n').filter(Boolean);
          for (const line of lines) {
            try {
              const parsed = JSON.parse(line);
              const text = parsed.message?.content || '';
              if (text) { full += text; onChunk(text); }
            } catch (_) {}
          }
        });
        res.on('end', () => resolve(full));
        res.on('error', reject);
      }
    );
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ---------------------------------------------------------------------------
// Database
// ---------------------------------------------------------------------------
const DATA_DIR = path.join(__dirname, 'data');
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
const db = new Database(path.join(DATA_DIR, 'sessions.db'));
db.exec(`
  CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT 'New conversation',
    messages     TEXT NOT NULL DEFAULT '[]',
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
    system: `You are the Manager agent in a multi-agent AI system. Your role is to:
1. Understand the user's request carefully
2. Decide if you need specialist agents or can answer directly
3. If specialists are needed, delegate using this exact format:
<delegate agent="coder">specific task</delegate>
<delegate agent="researcher">specific task</delegate>
<delegate agent="writer">specific task</delegate>

Available specialists:
- coder: code writing, debugging, code review, technical implementation
- researcher: research, summarizing, finding information, analysis
- writer: writing content, documentation, editing, commit messages

For simple questions, answer directly without delegating.
For complex tasks, delegate clearly and the system will synthesize the results.
Keep your planning brief — just delegate or answer.`,
  },
  coder: {
    name: 'Coder',
    emoji: '💻',
    color: '#10b981',
    system: `You are the Coder agent. You specialize in writing clean, well-documented code, reviewing and improving existing code, debugging issues, and explaining technical concepts. Always use code blocks. Be precise and thorough.`,
  },
  researcher: {
    name: 'Researcher',
    emoji: '🔍',
    color: '#f59e0b',
    system: `You are the Researcher agent. You specialize in gathering and synthesizing information, summarizing topics clearly, providing relevant context and background, and structured analysis. Be thorough and well-organized.`,
  },
  writer: {
    name: 'Writer',
    emoji: '✍️',
    color: '#ec4899',
    system: `You are the Writer agent. You specialize in writing clear engaging content, technical documentation, commit messages and PR descriptions, and editing existing text. Match the appropriate tone and style. Be concise but complete.`,
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
// Health check
// ---------------------------------------------------------------------------
app.get('/health', (req, res) => {
  res.json({ ok: true, model: OLLAMA_MODEL, ollama: OLLAMA_HOST, agents: Object.keys(AGENTS) });
});

// Check if Ollama is running
app.get('/ollama/status', async (req, res) => {
  try {
    const result = await new Promise((resolve, reject) => {
      const url = new URL('/api/tags', OLLAMA_HOST);
      const req = http.get({ hostname: url.hostname, port: url.port || 80, path: url.pathname }, (r) => {
        let d = '';
        r.on('data', c => d += c);
        r.on('end', () => { try { resolve(JSON.parse(d)); } catch(e) { reject(e); } });
      });
      req.on('error', reject);
      req.setTimeout(3000, () => { req.destroy(); reject(new Error('timeout')); });
    });
    const models = (result.models || []).map(m => m.name);
    const hasModel = models.some(m => m.startsWith(OLLAMA_MODEL.split(':')[0]));
    res.json({ running: true, models, hasModel, currentModel: OLLAMA_MODEL });
  } catch (e) {
    res.json({ running: false, error: e.message });
  }
});

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------
app.get('/sessions', (req, res) => {
  const rows = db.prepare(`SELECT id, title, messages, last_accessed FROM sessions WHERE id LIKE 'ui-%' ORDER BY last_accessed DESC LIMIT 50`).all();
  res.json(rows.map(r => ({
    id: r.id, title: r.title,
    count: JSON.parse(r.messages).length,
    lastAccessed: r.last_accessed,
  })).filter(r => r.count > 0));
});

app.get('/sessions/:id/messages', (req, res) => {
  const row = stmtGet.get(req.params.id);
  res.json(row ? JSON.parse(row.messages) : []);
});

app.post('/sessions/:id/clear', (req, res) => {
  saveSession(req.params.id, 'New conversation', []);
  res.json({ ok: true });
});

// ---------------------------------------------------------------------------
// Main chat — Manager orchestrates specialists
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
  history.push({ role: 'user', content: message, agent: 'user', ts: Date.now() });
  send({ type: 'status', agent: 'manager', status: 'thinking' });

  try {
    // Step 1: Manager decides what to do
    const chatHistory = history
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .map(m => ({ role: m.role, content: m.content }));

    let managerResponse = '';
    await ollamaStream(
      AGENTS.manager.system,
      chatHistory,
      (chunk) => { managerResponse += chunk; send({ type: 'chunk', agent: 'manager', chunk }); }
    );

    // Step 2: Parse delegations
    const delegateRegex = /<delegate agent="(\w+)">(.*?)<\/delegate>/gs;
    const delegations = [];
    let match;
    while ((match = delegateRegex.exec(managerResponse)) !== null) {
      delegations.push({ agent: match[1], task: match[2].trim() });
    }

    // Step 3: Run specialists
    const specialistResults = {};
    for (const { agent, task } of delegations) {
      if (!AGENTS[agent]) continue;
      send({ type: 'status', agent, status: 'working' });
      let result = '';
      await ollamaStream(
        AGENTS[agent].system,
        [{ role: 'user', content: task }],
        (chunk) => { result += chunk; send({ type: 'chunk', agent, chunk }); }
      );
      specialistResults[agent] = result;
      send({ type: 'status', agent, status: 'done' });
    }

    // Step 4: Synthesize if there were delegations
    let finalResponse = managerResponse;
    if (delegations.length > 0) {
      send({ type: 'status', agent: 'manager', status: 'synthesizing' });
      const synthesisPrompt = `The user asked: "${message}"

Here are the specialist results:
${Object.entries(specialistResults).map(([a, r]) => `[${AGENTS[a].name}]:\n${r}`).join('\n\n')}

Now give the user a single clear, well-organized final answer that incorporates all the above.`;

      let synthesis = '';
      await ollamaStream(
        AGENTS.manager.system,
        [
          ...chatHistory,
          { role: 'assistant', content: managerResponse },
          { role: 'user', content: synthesisPrompt },
        ],
        (chunk) => { synthesis += chunk; send({ type: 'synthesis_chunk', agent: 'manager', chunk }); }
      );
      finalResponse = synthesis;
    }

    // Save session
    history.push({ role: 'assistant', content: finalResponse, agent: 'manager', ts: Date.now() });
    const title = sess.title === 'New conversation' ? message.slice(0, 60) : sess.title;
    saveSession(sessionId, title, history);

    send({ type: 'done', sessionId, delegations: delegations.map(d => d.agent) });
    Object.keys(AGENTS).forEach(a => send({ type: 'status', agent: a, status: 'idle' }));

  } catch (err) {
    console.error(err);
    const isOllamaDown = err.code === 'ECONNREFUSED' || err.message === 'timeout';
    send({ type: 'error', message: isOllamaDown ? 'Cannot connect to Ollama. Make sure it is running: ollama serve' : err.message });
  } finally {
    res.end();
  }
});

// ---------------------------------------------------------------------------
// GitHub routes
// ---------------------------------------------------------------------------
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
  console.log(`\n🚀 Claude Agent v3 (Ollama) running at http://localhost:${PORT}`);
  console.log(`   Model:  ${OLLAMA_MODEL}`);
  console.log(`   Ollama: ${OLLAMA_HOST}`);
  console.log(`   GitHub: ${GITHUB_TOKEN ? '✓ connected' : '✗ no token (add GITHUB_TOKEN to .env)'}`);
  console.log(`\n   Make sure Ollama is running: ollama serve`);
  console.log(`   And the model is pulled:     ollama pull ${OLLAMA_MODEL}\n`);
});
