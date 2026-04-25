import os
import json
import sqlite3
import re
import base64
import asyncio
import uuid
import httpx
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

OLLAMA_HOST  = os.getenv("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
PORT         = int(os.getenv("PORT", 3000))
GITHUB_USER  = "kale87"

class WSManager:
    def __init__(self):
        self.connections: list[WebSocket] = []
        self.pending_confirmations: dict[str, asyncio.Future] = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, event: dict):
        dead = []
        for ws in self.connections:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.connections:
                self.connections.remove(ws)

    async def agent_event(self, agent: str, status: str, message: str = ""):
        await self.broadcast({"type": "agent_status", "agent": agent, "status": status,
                               "message": message, "ts": datetime.utcnow().isoformat()})

    async def request_confirmation(self, confirm_id: str, action: str, details: str) -> bool:
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self.pending_confirmations[confirm_id] = future
        await self.broadcast({"type": "confirmation_request", "id": confirm_id,
                               "action": action, "details": details})
        try:
            return await asyncio.wait_for(future, timeout=60)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending_confirmations.pop(confirm_id, None)

    def resolve_confirmation(self, confirm_id: str, approved: bool):
        future = self.pending_confirmations.get(confirm_id)
        if future and not future.done():
            future.set_result(approved)

ws_manager = WSManager()

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "sessions.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

with get_db() as conn:
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'New conversation', messages TEXT NOT NULL DEFAULT '[]', last_accessed INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS skills (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, agent TEXT NOT NULL DEFAULT 'shared', content TEXT NOT NULL, created_at INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, agent_id TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'ok', created_at INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, task_id TEXT NOT NULL, project_name TEXT NOT NULL DEFAULT '', interaction_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'running', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)")
    conn.commit()

def audit(task_id: str, agent_id: str, action: str, details: dict = None, status: str = "ok"):
    now = int(datetime.now().timestamp() * 1000)
    with get_db() as conn:
        conn.execute("INSERT INTO audit_log (task_id,agent_id,action,details,status,created_at) VALUES (?,?,?,?,?,?)",
                     (task_id, agent_id, action, json.dumps(details or {}), status, now))
        conn.commit()

def telemetry_start(task_id: str, agent_id: str, project: str = "") -> int:
    now = int(datetime.now().timestamp() * 1000)
    with get_db() as conn:
        cur = conn.execute("INSERT INTO telemetry (agent_id,task_id,project_name,interaction_count,status,created_at,updated_at) VALUES (?,?,?,0,'running',?,?)",
                           (agent_id, task_id, project, now, now))
        conn.commit()
        return cur.lastrowid

def telemetry_done(row_id: int, status: str = "done"):
    now = int(datetime.now().timestamp() * 1000)
    with get_db() as conn:
        conn.execute("UPDATE telemetry SET status=?,updated_at=?,interaction_count=interaction_count+1 WHERE id=?",
                     (status, now, row_id))
        conn.commit()

def get_session(sid: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return {"title": row["title"], "messages": json.loads(row["messages"])} if row \
        else {"title": "New conversation", "messages": []}

def save_session(sid: str, title: str, messages: list):
    now = int(datetime.now().timestamp() * 1000)
    with get_db() as conn:
        conn.execute("INSERT INTO sessions (id,title,messages,last_accessed) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,messages=excluded.messages,last_accessed=excluded.last_accessed",
                     (sid, title, json.dumps(messages), now))
        conn.commit()

def get_skills(agent_key: str) -> str:
    with get_db() as conn:
        rows = conn.execute("SELECT name,content FROM skills WHERE agent='shared' OR agent=? ORDER BY created_at",
                            (agent_key,)).fetchall()
    if not rows:
        return ""
    return "\n\n---\n## Your Skills\n" + "\n\n".join(f"### {r['name']}\n{r['content']}" for r in rows)

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
AGENTS = {
    "orchestrator": {
        "name": "Orchestrator", "emoji": "\U0001f9e0", "color": "#6366f1",
        "system": """You are the Orchestrator of Kal-AI.
For SIMPLE questions \u2014 answer directly.
For TASKS \u2014 delegate:
  <delegate agent="coder">specific coding or GitHub task</delegate>
  <delegate agent="analyst">research, analysis, documentation task</delegate>
NEVER invent data. Only report what specialists return.""",
    },
    "coder": {
        "name": "Coder", "emoji": "\U0001f4bb", "color": "#10b981",
        "system": f"""You are the Coder agent. GitHub username: {GITHUB_USER}.
Write clean, working code. Always use fenced code blocks.

To commit code directly to GitHub, wrap your file content EXACTLY like this:
<commit repo="REPO_NAME" path="FILE_PATH" message="COMMIT_MESSAGE" branch="main">
FILE CONTENT HERE
</commit>

Example:
<commit repo="kal-ai" path="src/utils.py" message="Add utility functions">
def hello():
    return 'world'
</commit>

Always use <commit> when you write code that should be saved to a repo. Do NOT tell the user to commit manually.""",
    },
    "analyst": {
        "name": "Analyst", "emoji": "\U0001f50d", "color": "#f59e0b",
        "system": "You are the Analyst agent. Research topics, analyze information, produce clear structured summaries and documentation.",
    },
}
AGENT_KEYS = list(AGENTS.keys())

def build_system(agent_key: str) -> str:
    return AGENTS[agent_key]["system"] + get_skills(agent_key)

# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
EXCLUDED_WORDS = {
    'github','my','all','the','a','an','repo','repos','repository','repositories',
    'file','files','folder','branch','branches','code','list','show','get','read',
    'open','check','look','find','what','is','are','in','at','of','for','to','do',
    'i','me','please','can','you','could','would','main','master','latest','new',
    'create','make','add','push','pull','commit','merge','into','from','this',
    'that','with','and','or','on','it','its',
}

async def gh(method: str, endpoint: str, body: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "User-Agent": "kal-ai/1.0",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(method, f"https://api.github.com{endpoint}", headers=headers, json=body)
        if resp.status_code == 204:
            return {}
        data = resp.json()
        if resp.status_code >= 400:
            raise Exception(data.get("message", f"GitHub error {resp.status_code}"))
        return data

def extract_repo(message: str) -> Optional[str]:
    msg = message.lower()
    m = re.search(r'kale87/([\w.-]+)', message)
    if m:
        return m.group(1)
    for pattern in [
        r'(?:in|my|the|for|of)\s+([\w.-]+)\s+repo',
        r'([\w.-]+)\s+repo(?:sitory)?',
        r'repo(?:sitory)?\s+(?:called\s+|named\s+)?([\w.-]+)',
    ]:
        m = re.search(pattern, msg)
        if m:
            candidate = m.group(1).lower()
            if candidate not in EXCLUDED_WORDS and len(candidate) > 1:
                return candidate
    return None

def parse_github_intent(message: str):
    msg = message.lower().strip()
    if not re.search(r'\b(repos?|repositor(?:y|ies)|branch(?:es)?|commit|push|pr|merge|file|files|github|readme|pull.?request)\b', msg, re.I):
        return None, None
    repo = extract_repo(message)
    file_match = re.search(r'\b([\w/-]+\.[\w]{1,6})\b', msg)
    file_path = file_match.group(1) if file_match else None
    branch_m = re.search(r'(?:on\s+)?branch\s+([\w./-]+)', msg)
    branch = branch_m.group(1) if branch_m else ''
    if re.search(r'(create|make|new)\s+(?:a\s+)?branch', msg):
        bm = re.search(r'branch\s+(?:called\s+|named\s+)?["\']?([\w./-]+)["\']?', msg)
        bn = bm.group(1) if bm else None
        fm = re.search(r'from\s+([\w./-]+)', msg)
        fb = fm.group(1) if fm else 'main'
        if repo and bn and bn not in EXCLUDED_WORDS:
            return 'create_branch', {'owner': GITHUB_USER, 'repo': repo, 'branch': bn, 'from_branch': fb}
        return None, None
    if re.search(r'merge\s+(?:branch\s+)?', msg) and 'pull request' not in msg:
        hm = re.search(r'merge\s+(?:branch\s+)?["\']?([\w./-]+)["\']?\s+into', msg)
        bm = re.search(r'into\s+["\']?([\w./-]+)["\']?', msg)
        head = hm.group(1) if hm else None
        base = bm.group(1) if bm else 'main'
        if repo and head:
            return 'merge_branch', {'owner': GITHUB_USER, 'repo': repo, 'head': head, 'base': base, '_confirm': True}
        return None, None
    if re.search(r'(create|open|make)\s+(?:a\s+)?(?:pull.?request|pr)', msg):
        hm = re.search(r'from\s+["\']?([\w./-]+)["\']?', msg)
        bm = re.search(r'(?:to|into)\s+["\']?([\w./-]+)["\']?', msg)
        tm = re.search(r'(?:title|called|named)\s+["\']?(.+?)["\']?(?:\s+in|\s+for|$)', msg)
        head = hm.group(1) if hm else None
        base = bm.group(1) if bm else 'main'
        title = tm.group(1) if tm else f'PR from {head} into {base}'
        if repo and head:
            return 'create_pr', {'owner': GITHUB_USER, 'repo': repo, 'head': head, 'base': base, 'title': title, '_confirm': True}
        return None, None
    if re.search(r'merge\s+(?:pull.?request|pr)', msg):
        nm = re.search(r'#?(\d+)', msg)
        number = int(nm.group(1)) if nm else None
        if repo and number:
            return 'merge_pr', {'owner': GITHUB_USER, 'repo': repo, 'number': number, '_confirm': True}
        return None, None
    if not repo and re.search(r'(list|show|get|what).{0,30}repos?', msg):
        return 'list_repos', {}
    if not repo and re.search(r'\brepos?\b', msg):
        return 'list_repos', {}
    if repo and re.search(r'(list|show|files?|contents?|inside|browse|explore|what.{0,10}in)', msg):
        pm = re.search(r'(?:in|inside|under)\s+(?:the\s+)?([\w/-]+)\s+(?:folder|directory|dir)', msg)
        return 'list_files', {'owner': GITHUB_USER, 'repo': repo, 'path': pm.group(1) if pm else '', 'branch': branch}
    if repo and file_path:
        return 'read_file', {'owner': GITHUB_USER, 'repo': repo, 'path': file_path, 'branch': branch}
    if repo and re.search(r'branch(es)?', msg):
        return 'list_branches', {'owner': GITHUB_USER, 'repo': repo}
    if repo and re.search(r'(pull.?request|\bpr\b)', msg):
        return 'list_prs', {'owner': GITHUB_USER, 'repo': repo, 'state': 'open'}
    if repo:
        return 'list_files', {'owner': GITHUB_USER, 'repo': repo, 'path': '', 'branch': ''}
    return None, None

async def execute_intent(intent: str, params: dict, task_id: str, needs_confirm: bool = False) -> str:
    p = {k: v for k, v in params.items() if not k.startswith('_')}
    if needs_confirm:
        confirm_id = str(uuid.uuid4())
        approved = await ws_manager.request_confirmation(confirm_id, intent.replace('_', ' ').title(), json.dumps(p, indent=2))
        if not approved:
            audit(task_id, 'coder', intent, p, 'rejected')
            return f"\u274c Action `{intent}` was rejected."
    try:
        result = await _run_gh_intent(intent, p)
        audit(task_id, 'coder', intent, p, 'ok')
        return result
    except Exception as e:
        audit(task_id, 'coder', intent, p, 'error')
        return f"\u274c Error: {str(e)}"

async def _run_gh_intent(intent: str, p: dict) -> str:
    if intent == 'list_repos':
        data = await gh("GET", "/user/repos?sort=updated&per_page=50")
        lines = ["**Your GitHub repositories:**"]
        for r in data:
            priv = " \U0001f512" if r.get("private") else ""
            desc = f" \u2014 {r['description']}" if r.get("description") else ""
            lines.append(f"- **{r['name']}**{priv}{desc}")
        return "\n".join(lines)
    elif intent == 'list_files':
        owner, repo = p['owner'], p['repo']
        path = p.get('path', '').lstrip('/')
        branch = p.get('branch', '')
        ep = f"/repos/{owner}/{repo}/contents/{path}"
        if branch:
            ep += f"?ref={branch}"
        data = await gh("GET", ep)
        if isinstance(data, list):
            display = f"{repo}/{path}" if path else repo
            lines = [f"**Files in `{display}`:**"]
            for f in sorted(data, key=lambda x: (x['type'] != 'dir', x['name'])):
                icon = "\U0001f4c1" if f['type'] == 'dir' else "\U0001f4c4"
                lines.append(f"- {icon} `{f['name']}`")
            return "\n".join(lines)
        return "Not a directory."
    elif intent == 'read_file':
        owner, repo, path = p['owner'], p['repo'], p['path']
        branch = p.get('branch', '')
        ep = f"/repos/{owner}/{repo}/contents/{path}"
        if branch:
            ep += f"?ref={branch}"
        data = await gh("GET", ep)
        content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
        ext = path.split('.')[-1] if '.' in path else ''
        truncated = content[:4000] + ("\n..." if len(content) > 4000 else "")
        return f"**`{path}`:**\n```{ext}\n{truncated}\n```"
    elif intent == 'list_branches':
        data = await gh("GET", f"/repos/{p['owner']}/{p['repo']}/branches")
        lines = [f"**Branches in `{p['repo']}`:**"]
        for b in data:
            lines.append(f"- `{b['name']}`")
        return "\n".join(lines)
    elif intent == 'list_prs':
        state = p.get('state', 'open')
        data = await gh("GET", f"/repos/{p['owner']}/{p['repo']}/pulls?state={state}&per_page=20")
        lines = [f"**{state.title()} PRs in `{p['repo']}`:**"]
        if not data:
            lines.append("No PRs found.")
        for pr in data:
            lines.append(f"- #{pr['number']} **{pr['title']}** (`{pr['head']['ref']}` \u2192 `{pr['base']['ref']}`)") 
        return "\n".join(lines)
    elif intent == 'create_branch':
        ref_data = await gh("GET", f"/repos/{p['owner']}/{p['repo']}/git/ref/heads/{p.get('from_branch', 'main')}")
        sha = ref_data["object"]["sha"]
        await gh("POST", f"/repos/{p['owner']}/{p['repo']}/git/refs",
                 {"ref": f"refs/heads/{p['branch']}", "sha": sha})
        return f"\u2705 Branch `{p['branch']}` created from `{p.get('from_branch', 'main')}` in `{p['repo']}`"
    elif intent == 'merge_branch':
        result = await gh("POST", f"/repos/{p['owner']}/{p['repo']}/merges",
                          {"base": p['base'], "head": p['head'],
                           "commit_message": f"Merge {p['head']} into {p['base']}"})
        return f"\u2705 Merged `{p['head']}` into `{p['base']}` (commit `{result.get('sha', '')[:7]}`)"
    elif intent == 'create_pr':
        result = await gh("POST", f"/repos/{p['owner']}/{p['repo']}/pulls",
                          {"title": p['title'], "head": p['head'],
                           "base": p.get('base', 'main'), "body": p.get('body', '')})
        return f"\u2705 PR #{result['number']} created: [{result['title']}]({result['html_url']})"
    elif intent == 'merge_pr':
        result = await gh("PUT", f"/repos/{p['owner']}/{p['repo']}/pulls/{p['number']}/merge",
                          {"merge_method": "squash"})
        return f"\u2705 PR #{p['number']} merged (commit `{result.get('sha', '')[:7]}`)"
    elif intent == 'commit_file':
        branch = p.get('branch', 'main')
        body: dict = {
            "message": p.get('message', f"Update {p['path']}"),
            "content": base64.b64encode(p['content'].encode()).decode(),
            "branch": branch,
        }
        try:
            existing = await gh("GET", f"/repos/{p['owner']}/{p['repo']}/contents/{p['path']}?ref={branch}")
            body["sha"] = existing["sha"]
        except Exception:
            pass
        result = await gh("PUT", f"/repos/{p['owner']}/{p['repo']}/contents/{p['path']}", body)
        sha = result.get('commit', {}).get('sha', '')[:7]
        return f"\u2705 Committed `{p['path']}` to `{branch}` in `{p['repo']}` (commit `{sha}`)"
    return f"Unknown intent: {intent}"

async def auto_commit_from_response(response: str, task_id: str) -> list:
    """
    Scan coder response for <commit> tags and push each file to GitHub automatically.
    """
    pattern = re.compile(
        r'<commit\s+repo="([^"]+)"\s+path="([^"]+)"\s+message="([^"]+)"(?:\s+branch="([^"]+)")?>(.*?)</commit>',
        re.DOTALL
    )
    results = []
    for m in pattern.finditer(response):
        repo, path, message, branch, content = (
            m.group(1), m.group(2), m.group(3),
            m.group(4) or 'main', m.group(5).strip()
        )
        params = {'owner': GITHUB_USER, 'repo': repo, 'path': path,
                  'content': content, 'message': message, 'branch': branch}
        try:
            result = await _run_gh_intent('commit_file', params)
            audit(task_id, 'coder', 'commit_file', params, 'ok')
            results.append(result)
        except Exception as e:
            audit(task_id, 'coder', 'commit_file', params, 'error')
            results.append(f"\u274c Commit failed for `{path}`: {str(e)}")
    return results

# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
async def ollama_stream(system: str, messages: list) -> AsyncGenerator[str, None]:
    payload = {"model": OLLAMA_MODEL,
               "messages": [{"role": "system", "content": system}] + messages,
               "stream": True}
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", f"{OLLAMA_HOST}/api/chat", json=payload) as resp:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line).get("message", {}).get("content", "")
                    if chunk:
                        yield chunk
                except Exception:
                    pass

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Kal-AI")

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "confirmation_response":
                ws_manager.resolve_confirmation(data["id"], data["approved"])
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)

@app.get("/health")
async def health():
    return {"ok": True, "model": OLLAMA_MODEL, "agents": AGENT_KEYS}

@app.get("/ollama/status")
async def ollama_status():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            data = (await client.get(f"{OLLAMA_HOST}/api/tags")).json()
        models = [m["name"] for m in data.get("models", [])]
        return {"running": True, "models": models,
                "hasModel": any(m.startswith(OLLAMA_MODEL.split(":")[0]) for m in models),
                "currentModel": OLLAMA_MODEL}
    except Exception as e:
        return {"running": False, "error": str(e)}

@app.get("/sessions")
async def list_sessions():
    with get_db() as conn:
        rows = conn.execute("SELECT id,title,messages,last_accessed FROM sessions WHERE id LIKE 'ui-%' ORDER BY last_accessed DESC LIMIT 50").fetchall()
    return [{"id": r["id"], "title": r["title"], "count": len(json.loads(r["messages"])), "lastAccessed": r["last_accessed"]}
            for r in rows if json.loads(r["messages"])]

@app.get("/sessions/{sid}/messages")
async def get_messages(sid: str):
    return get_session(sid)["messages"]

@app.post("/sessions/{sid}/clear")
async def clear_session(sid: str):
    save_session(sid, "New conversation", [])
    return {"ok": True}

@app.get("/skills")
async def list_skills(agent: Optional[str] = None):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM skills WHERE agent=? OR agent='shared' ORDER BY created_at" if agent
            else "SELECT * FROM skills ORDER BY created_at",
            (agent,) if agent else ()).fetchall()
    return [dict(r) for r in rows]

@app.post("/skills")
async def create_skill(request: Request):
    b = await request.json()
    name, agent, content = b.get("name", "").strip(), b.get("agent", "shared").strip(), b.get("content", "").strip()
    if not name or not content:
        return JSONResponse({"error": "name and content required"}, status_code=400)
    now = int(datetime.now().timestamp() * 1000)
    with get_db() as conn:
        cur = conn.execute("INSERT INTO skills (name,agent,content,created_at) VALUES (?,?,?,?)",
                           (name, agent, content, now))
        conn.commit()
    return {"id": cur.lastrowid, "name": name, "agent": agent, "content": content, "created_at": now}

@app.put("/skills/{skill_id}")
async def update_skill(skill_id: int, request: Request):
    b = await request.json()
    with get_db() as conn:
        conn.execute("UPDATE skills SET name=?,content=?,agent=? WHERE id=?",
                     (b.get("name", "").strip(), b.get("content", "").strip(), b.get("agent", "shared").strip(), skill_id))
        conn.commit()
    return {"ok": True}

@app.delete("/skills/{skill_id}")
async def delete_skill(skill_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        conn.commit()
    return {"ok": True}

@app.get("/audit")
async def get_audit(limit: int = 50):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

@app.get("/telemetry")
async def get_telemetry():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM telemetry ORDER BY created_at DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    message = body.get("message", "").strip()
    session_id = body.get("sessionId", "default")
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)
    task_id = str(uuid.uuid4())

    async def generate():
        def sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        sess = get_session(session_id)
        history = sess["messages"]
        history.append({"role": "user", "content": message, "agent": "user"})
        chat_msgs = [{"role": m["role"], "content": m["content"]}
                     for m in history if m["role"] in ("user", "assistant")]

        # FAST PATH: direct GitHub intent
        intent, params = parse_github_intent(message)
        if intent:
            await ws_manager.agent_event("coder", "walking", "Heading to GitHub...")
            yield sse({"type": "status", "agent": "coder", "status": "working"})
            yield sse({"type": "tool_call", "agent": "coder", "tool": intent, "params": params})
            needs_confirm = params.get('_confirm', False)
            tel_id = telemetry_start(task_id, "coder")
            await ws_manager.agent_event("coder", "coding", f"Running: {intent}")
            result = await execute_intent(intent, params, task_id, needs_confirm)
            await ws_manager.agent_event("coder", "done", "Finished.")
            telemetry_done(tel_id)
            yield sse({"type": "tool_result", "agent": "coder", "tool": intent, "result": result})
            yield sse({"type": "chunk", "agent": "coder", "chunk": result})
            history.append({"role": "assistant", "content": result, "agent": "coder"})
            title = message[:60] if sess["title"] == "New conversation" else sess["title"]
            save_session(session_id, title, history)
            yield sse({"type": "status", "agent": "coder", "status": "done"})
            yield sse({"type": "done", "sessionId": session_id, "delegations": ["coder"]})
            await ws_manager.agent_event("coder", "idle")
            for k in AGENT_KEYS:
                yield sse({"type": "status", "agent": k, "status": "idle"})
            return

        # NORMAL PATH: Orchestrator
        await ws_manager.agent_event("orchestrator", "thinking", "Analyzing request...")
        yield sse({"type": "status", "agent": "orchestrator", "status": "thinking"})
        manager_response = ""
        async for chunk in ollama_stream(build_system("orchestrator"), chat_msgs):
            manager_response += chunk
            yield sse({"type": "chunk", "agent": "orchestrator", "chunk": chunk})

        delegations = re.findall(r'<delegate agent="(\w+)">(.*?)</delegate>', manager_response, re.DOTALL)

        if not delegations:
            history.append({"role": "assistant", "content": manager_response, "agent": "orchestrator"})
            title = message[:60] if sess["title"] == "New conversation" else sess["title"]
            save_session(session_id, title, history)
            await ws_manager.agent_event("orchestrator", "idle")
            yield sse({"type": "done", "sessionId": session_id, "delegations": []})
            for k in AGENT_KEYS:
                yield sse({"type": "status", "agent": k, "status": "idle"})
            return

        specialist_results: dict = {}
        for agent_key, task in delegations:
            if agent_key not in AGENTS:
                continue
            await ws_manager.agent_event(agent_key, "walking", "Walking to desk...")
            yield sse({"type": "status", "agent": agent_key, "status": "working"})
            await ws_manager.agent_event(agent_key,
                                         "coding" if agent_key == "coder" else "searching", task[:60])
            tel_id = telemetry_start(task_id, agent_key)
            result = ""
            async for chunk in ollama_stream(build_system(agent_key),
                                             [{"role": "user", "content": task.strip()}]):
                result += chunk
                yield sse({"type": "chunk", "agent": agent_key, "chunk": chunk})

            # AUTO-COMMIT: parse <commit> tags from coder response and push to GitHub
            if agent_key == "coder":
                await ws_manager.agent_event("coder", "coding", "Scanning for commits...")
                commit_results = await auto_commit_from_response(result, task_id)
                for cr in commit_results:
                    await ws_manager.agent_event("coder", "coding", "Committing to GitHub...")
                    result += "\n\n" + cr
                    yield sse({"type": "chunk", "agent": "coder", "chunk": "\n\n" + cr})

            specialist_results[agent_key] = result
            telemetry_done(tel_id)
            await ws_manager.agent_event(agent_key, "done", "Task complete.")
            yield sse({"type": "status", "agent": agent_key, "status": "done"})

        # Synthesize
        await ws_manager.agent_event("orchestrator", "thinking", "Synthesizing results...")
        yield sse({"type": "status", "agent": "orchestrator", "status": "synthesizing"})
        summary = "\n\n".join(f"[{AGENTS[k]['name']}]:\n{v}" for k, v in specialist_results.items())
        synth_msgs = chat_msgs + [
            {"role": "assistant", "content": manager_response},
            {"role": "user", "content": f'User asked: "{message}"\n\nSpecialist results:\n{summary}\n\nPresent clearly.'},
        ]
        synthesis = ""
        async for chunk in ollama_stream(build_system("orchestrator"), synth_msgs):
            synthesis += chunk
            yield sse({"type": "synthesis_chunk", "agent": "orchestrator", "chunk": chunk})

        history.append({"role": "assistant", "content": synthesis, "agent": "orchestrator"})
        title = message[:60] if sess["title"] == "New conversation" else sess["title"]
        save_session(session_id, title, history)
        await ws_manager.agent_event("orchestrator", "idle")
        yield sse({"type": "done", "sessionId": session_id, "delegations": [k for k, _ in delegations]})
        for k in AGENT_KEYS:
            yield sse({"type": "status", "agent": k, "status": "idle"})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.get("/github/repos")
async def gh_repos():
    try: return await gh("GET", "/user/repos?sort=updated&per_page=30")
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/github/repos/{owner}/{repo}/contents")
async def gh_contents(owner: str, repo: str, path: str = ""):
    try: return await gh("GET", f"/repos/{owner}/{repo}/contents/{path}")
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/github/repos/{owner}/{repo}/branches")
async def gh_branches(owner: str, repo: str):
    try: return await gh("GET", f"/repos/{owner}/{repo}/branches")
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/github/repos/{owner}/{repo}/pulls")
async def gh_pulls(owner: str, repo: str):
    try: return await gh("GET", f"/repos/{owner}/{repo}/pulls?state=open")
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/github/repos/{owner}/{repo}/commits")
async def gh_commit(owner: str, repo: str, request: Request):
    try:
        b = await request.json()
        payload = {"message": b["message"],
                   "content": base64.b64encode(b["content"].encode()).decode(),
                   "branch": b.get("branch", "main")}
        if b.get("sha"): payload["sha"] = b["sha"]
        return await gh("PUT", f"/repos/{owner}/{repo}/contents/{b['path']}", payload)
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/github/repos/{owner}/{repo}/pulls")
async def gh_create_pr(owner: str, repo: str, request: Request):
    try: return await gh("POST", f"/repos/{owner}/{repo}/pulls", await request.json())
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

app.mount("/", StaticFiles(directory="public", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print(f"""
\U0001f680 Kal-AI  http://localhost:{PORT}
   Model : {OLLAMA_MODEL}
   GitHub: {'connected' if GITHUB_TOKEN else 'NO TOKEN'}
   WS    : ws://localhost:{PORT}/ws
   Tabs  : Chat | Agent Office | Dashboard | Audit Log

   Make sure Ollama is running: ollama serve
""")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
