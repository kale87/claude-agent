import os
import json
import sqlite3
import re
import base64
import httpx
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

OLLAMA_HOST  = os.getenv("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
PORT         = int(os.getenv("PORT", 3000))
GITHUB_USER  = "kale87"

# Words that should never be treated as repo names
EXCLUDED_WORDS = {
    'github', 'my', 'all', 'the', 'a', 'an', 'repo', 'repos', 'repository',
    'repositories', 'file', 'files', 'folder', 'branch', 'branches', 'code',
    'list', 'show', 'get', 'read', 'open', 'check', 'look', 'find', 'what',
    'is', 'are', 'in', 'at', 'of', 'for', 'to', 'do', 'i', 'me', 'please',
    'can', 'you', 'could', 'would', 'main', 'master', 'latest', 'new',
    'create', 'make', 'add', 'push', 'pull', 'commit', 'merge', 'into',
    'from', 'this', 'that', 'with', 'and', 'or', 'on', 'it', 'its',
}

async def gh(method: str, endpoint: str, body: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "User-Agent": "kal-ai/1.0",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(method, f"https://api.github.com{endpoint}", headers=headers, json=body)
        if resp.status_code == 204: return {}
        data = resp.json()
        if resp.status_code >= 400: raise Exception(data.get("message", f"GitHub error {resp.status_code}"))
        return data

def extract_repo(message: str):
    """Extract a valid repo name from a message."""
    msg = message.lower()
    # Explicit owner/repo
    m = re.search(r'kale87/([\w.-]+)', message)
    if m: return m.group(1)
    # Patterns like "in X repo", "X repo", "repo X"
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

def extract_branch_name(message: str, exclude: list = []):
    """Extract a branch name from a message."""
    msg = message.lower()
    # Patterns: "branch X", "branch called X", "X branch"
    for pattern in [
        r'branch\s+(?:called\s+|named\s+)?["\']?([\w./-]+)["\']?',
        r'["\']([\w./-]+)["\']\s+branch',
    ]:
        m = re.search(pattern, msg)
        if m:
            candidate = m.group(1)
            if candidate not in EXCLUDED_WORDS and candidate not in exclude:
                return candidate
    return None

# ---------------------------------------------------------------------------
# Intent parser
# ---------------------------------------------------------------------------
def parse_github_intent(message: str):
    msg = message.lower().strip()

    # Must mention a GitHub concept
    if not re.search(r'\b(repos?|repositor(?:y|ies)|branch(?:es)?|commit|push|pr|merge|file|files|github|readme|pull.?request)\b', msg, re.I):
        return None, None

    repo  = extract_repo(message)
    
    # File path
    file_match = re.search(r'\b([\w/-]+\.[\w]{1,6})\b', msg)
    file_path  = file_match.group(1) if file_match else None

    # ----------------------------------------------------------------
    # WRITE INTENTS
    # ----------------------------------------------------------------

    # CREATE BRANCH — "create branch X in repo Y", "make branch X"
    if re.search(r'(create|make|new)\s+(?:a\s+)?branch', msg):
        branch = extract_branch_name(message)
        from_branch_m = re.search(r'from\s+([\w./-]+)', msg)
        from_branch = from_branch_m.group(1) if from_branch_m else 'main'
        if repo and branch:
            return 'create_branch', {'owner': GITHUB_USER, 'repo': repo, 'branch': branch, 'from_branch': from_branch}
        return None, None

    # MERGE BRANCH — "merge X into Y in repo Z"
    if re.search(r'merge\s+(?:branch\s+)?', msg) and 'pull request' not in msg and 'pr' not in msg.split():
        head_m = re.search(r'merge\s+(?:branch\s+)?["\']?([\w./-]+)["\']?\s+into', msg)
        base_m = re.search(r'into\s+["\']?([\w./-]+)["\']?', msg)
        head = head_m.group(1) if head_m else None
        base = base_m.group(1) if base_m else 'main'
        if repo and head:
            return 'merge_branch', {'owner': GITHUB_USER, 'repo': repo, 'head': head, 'base': base}
        return None, None

    # CREATE PR — "create/open PR from X to Y in repo Z"
    if re.search(r'(create|open|make)\s+(?:a\s+)?(?:pull.?request|pr)', msg):
        head_m = re.search(r'from\s+["\']?([\w./-]+)["\']?', msg)
        base_m = re.search(r'(?:to|into)\s+["\']?([\w./-]+)["\']?', msg)
        title_m = re.search(r'(?:title|called|named)\s+["\']?(.+?)["\']?(?:\s+in|\s+for|$)', msg)
        head  = head_m.group(1) if head_m else None
        base  = base_m.group(1) if base_m else 'main'
        title = title_m.group(1) if title_m else f'PR from {head} into {base}'
        if repo and head:
            return 'create_pr', {'owner': GITHUB_USER, 'repo': repo, 'head': head, 'base': base, 'title': title}
        return None, None

    # MERGE PR — "merge PR #5 in repo X"
    if re.search(r'merge\s+(?:pull.?request|pr)', msg):
        num_m = re.search(r'#?(\d+)', msg)
        number = int(num_m.group(1)) if num_m else None
        if repo and number:
            return 'merge_pr', {'owner': GITHUB_USER, 'repo': repo, 'number': number}
        return None, None

    # ----------------------------------------------------------------
    # READ INTENTS
    # ----------------------------------------------------------------

    # LIST REPOS
    if not repo and re.search(r'(list|show|get|what).{0,30}repos?', msg):
        return 'list_repos', {}
    if not repo and re.search(r'\brepos?\b', msg):
        return 'list_repos', {}

    # LIST FILES
    if repo and re.search(r'(list|show|files?|contents?|inside|browse|explore|what.{0,10}in)', msg):
        path_match = re.search(r'(?:in|inside|under)\s+(?:the\s+)?([\w/-]+)\s+(?:folder|directory|dir)', msg)
        path = path_match.group(1) if path_match else ''
        branch_m = re.search(r'(?:on\s+)?branch\s+([\w./-]+)', msg)
        branch = branch_m.group(1) if branch_m else ''
        return 'list_files', {'owner': GITHUB_USER, 'repo': repo, 'path': path, 'branch': branch}

    # READ FILE
    if repo and file_path:
        branch_m = re.search(r'(?:on\s+)?branch\s+([\w./-]+)', msg)
        branch = branch_m.group(1) if branch_m else ''
        return 'read_file', {'owner': GITHUB_USER, 'repo': repo, 'path': file_path, 'branch': branch}

    # LIST BRANCHES
    if repo and re.search(r'branch(es)?', msg):
        return 'list_branches', {'owner': GITHUB_USER, 'repo': repo}

    # LIST PRs
    if repo and re.search(r'(pull.?request|\bpr\b)', msg):
        return 'list_prs', {'owner': GITHUB_USER, 'repo': repo, 'state': 'open'}

    # Repo mentioned — default to list files
    if repo:
        return 'list_files', {'owner': GITHUB_USER, 'repo': repo, 'path': '', 'branch': ''}

    return None, None

# ---------------------------------------------------------------------------
# Intent executor
# ---------------------------------------------------------------------------
async def execute_intent(intent: str, params: dict) -> str:
    try:
        if intent == 'list_repos':
            data = await gh("GET", "/user/repos?sort=updated&per_page=50")
            lines = ["**Your GitHub repositories:**"]
            for r in data:
                priv = " \U0001f512" if r.get("private") else ""
                desc = f" \u2014 {r['description']}" if r.get("description") else ""
                lines.append(f"- **{r['name']}**{priv}{desc}")
            return "\n".join(lines)

        elif intent == 'list_files':
            owner, repo = params['owner'], params['repo']
            path   = params.get('path', '').lstrip('/')
            branch = params.get('branch', '')
            ep = f"/repos/{owner}/{repo}/contents/{path}"
            if branch: ep += f"?ref={branch}"
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
            owner, repo, path = params['owner'], params['repo'], params['path']
            branch = params.get('branch', '')
            ep = f"/repos/{owner}/{repo}/contents/{path}"
            if branch: ep += f"?ref={branch}"
            data = await gh("GET", ep)
            content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
            ext = path.split('.')[-1] if '.' in path else ''
            truncated = content[:4000] + ("\n..." if len(content) > 4000 else "")
            return f"**`{path}`:**\n```{ext}\n{truncated}\n```"

        elif intent == 'list_branches':
            owner, repo = params['owner'], params['repo']
            data = await gh("GET", f"/repos/{owner}/{repo}/branches")
            lines = [f"**Branches in `{repo}`:**"]
            for b in data: lines.append(f"- `{b['name']}`")
            return "\n".join(lines)

        elif intent == 'list_prs':
            owner, repo = params['owner'], params['repo']
            state = params.get('state', 'open')
            data = await gh("GET", f"/repos/{owner}/{repo}/pulls?state={state}&per_page=20")
            lines = [f"**{'Open' if state=='open' else state.title()} PRs in `{repo}`:**"]
            if not data: lines.append("No PRs found.")
            for p in data: lines.append(f"- #{p['number']} **{p['title']}** (`{p['head']['ref']}` \u2192 `{p['base']['ref']}`)") 
            return "\n".join(lines)

        elif intent == 'create_branch':
            owner, repo = params['owner'], params['repo']
            branch      = params['branch']
            from_branch = params.get('from_branch', 'main')
            ref_data = await gh("GET", f"/repos/{owner}/{repo}/git/ref/heads/{from_branch}")
            sha = ref_data["object"]["sha"]
            await gh("POST", f"/repos/{owner}/{repo}/git/refs",
                     {"ref": f"refs/heads/{branch}", "sha": sha})
            return f"\u2705 Branch `{branch}` created from `{from_branch}` in `{repo}`"

        elif intent == 'merge_branch':
            owner, repo = params['owner'], params['repo']
            head, base  = params['head'], params.get('base', 'main')
            result = await gh("POST", f"/repos/{owner}/{repo}/merges",
                              {"base": base, "head": head,
                               "commit_message": f"Merge {head} into {base}"})
            sha = result.get('sha', '')[:7]
            return f"\u2705 Merged `{head}` into `{base}` in `{repo}` (commit `{sha}`)"

        elif intent == 'create_pr':
            owner, repo = params['owner'], params['repo']
            result = await gh("POST", f"/repos/{owner}/{repo}/pulls",
                              {"title": params['title'], "head": params['head'],
                               "base": params.get('base', 'main'), "body": params.get('body', '')})
            return f"\u2705 PR #{result['number']} created: [{result['title']}]({result['html_url']})"

        elif intent == 'merge_pr':
            owner, repo, number = params['owner'], params['repo'], params['number']
            result = await gh("PUT", f"/repos/{owner}/{repo}/pulls/{number}/merge",
                              {"merge_method": "squash"})
            sha = result.get('sha', '')[:7]
            return f"\u2705 PR #{number} merged (commit `{sha}`)"

        elif intent == 'commit_file':
            owner, repo  = params['owner'], params['repo']
            path, content = params['path'], params['content']
            message = params.get('message', f'Update {path}')
            branch  = params.get('branch', 'main')
            body: dict = {
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "branch":  branch,
            }
            try:
                existing = await gh("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}")
                body["sha"] = existing["sha"]
            except Exception:
                pass
            result = await gh("PUT", f"/repos/{owner}/{repo}/contents/{path}", body)
            sha = result.get('commit', {}).get('sha', '')[:7]
            return f"\u2705 Committed `{path}` to `{branch}` in `{repo}` (commit `{sha}`)"

        else:
            return f"Unknown intent: {intent}"

    except Exception as e:
        return f"\u274c Error: {str(e)}"

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
AGENTS = {
    "manager": {"name":"Manager","emoji":"🎯","color":"#6366f1","system":"""You are the Manager agent of Kal-AI.\nFor simple questions, answer directly.\nFor complex tasks, delegate:\n<delegate agent=\"coder\">task</delegate>\n<delegate agent=\"researcher\">task</delegate>\n<delegate agent=\"writer\">task</delegate>\nNEVER invent information."""},
    "coder":      {"name":"Coder",     "emoji":"💻","color":"#10b981","system":"You are the Coder agent. Write clean code, fix bugs, and explain technical concepts clearly."},
    "researcher": {"name":"Researcher","emoji":"🔍","color":"#f59e0b","system":"You are the Researcher agent. Gather and analyze information clearly."},
    "writer":     {"name":"Writer",    "emoji":"✍️", "color":"#ec4899","system":"You are the Writer agent. Write clear documentation and content."},
}
AGENT_KEYS = list(AGENTS.keys())

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
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
    conn.commit()

def get_session(sid):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return {"title":row["title"],"messages":json.loads(row["messages"])} if row else {"title":"New conversation","messages":[]}

def save_session(sid, title, messages):
    now = int(datetime.now().timestamp()*1000)
    with get_db() as conn:
        conn.execute("INSERT INTO sessions (id,title,messages,last_accessed) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,messages=excluded.messages,last_accessed=excluded.last_accessed", (sid,title,json.dumps(messages),now))
        conn.commit()

def get_skills(agent_key):
    with get_db() as conn:
        rows = conn.execute("SELECT name,content FROM skills WHERE agent='shared' OR agent=? ORDER BY created_at", (agent_key,)).fetchall()
    if not rows: return ""
    return "\n\n---\n## Your Skills\n" + "\n\n".join(f"### {r['name']}\n{r['content']}" for r in rows)

def build_system(agent_key):
    return AGENTS[agent_key]["system"] + get_skills(agent_key)

async def ollama_stream(system, messages) -> AsyncGenerator[str, None]:
    payload = {"model":OLLAMA_MODEL,"messages":[{"role":"system","content":system}]+messages,"stream":True}
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", f"{OLLAMA_HOST}/api/chat", json=payload) as resp:
            async for line in resp.aiter_lines():
                if not line: continue
                try:
                    chunk = json.loads(line).get("message",{}).get("content","")
                    if chunk: yield chunk
                except: pass

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Kal-AI")

@app.get("/health")
async def health():
    return {"ok":True,"model":OLLAMA_MODEL,"ollama":OLLAMA_HOST,"agents":AGENT_KEYS}

@app.get("/ollama/status")
async def ollama_status():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            data = (await client.get(f"{OLLAMA_HOST}/api/tags")).json()
        models = [m["name"] for m in data.get("models",[])]
        return {"running":True,"models":models,"hasModel":any(m.startswith(OLLAMA_MODEL.split(":")[0]) for m in models),"currentModel":OLLAMA_MODEL}
    except Exception as e:
        return {"running":False,"error":str(e)}

@app.get("/sessions")
async def list_sessions():
    with get_db() as conn:
        rows = conn.execute("SELECT id,title,messages,last_accessed FROM sessions WHERE id LIKE 'ui-%' ORDER BY last_accessed DESC LIMIT 50").fetchall()
    return [{"id":r["id"],"title":r["title"],"count":len(json.loads(r["messages"])),"lastAccessed":r["last_accessed"]} for r in rows if json.loads(r["messages"])]

@app.get("/sessions/{sid}/messages")
async def get_messages(sid: str):
    return get_session(sid)["messages"]

@app.post("/sessions/{sid}/clear")
async def clear_session(sid: str):
    save_session(sid, "New conversation", [])
    return {"ok":True}

@app.get("/skills")
async def list_skills(agent: Optional[str] = None):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM skills WHERE agent=? OR agent='shared' ORDER BY created_at" if agent else "SELECT * FROM skills ORDER BY created_at", (agent,) if agent else ()).fetchall()
    return [dict(r) for r in rows]

@app.post("/skills")
async def create_skill(request: Request):
    b = await request.json()
    name,agent,content = b.get("name","").strip(),b.get("agent","shared").strip(),b.get("content","").strip()
    if not name or not content: return JSONResponse({"error":"name and content required"},status_code=400)
    now = int(datetime.now().timestamp()*1000)
    with get_db() as conn:
        cur = conn.execute("INSERT INTO skills (name,agent,content,created_at) VALUES (?,?,?,?)",(name,agent,content,now))
        conn.commit()
    return {"id":cur.lastrowid,"name":name,"agent":agent,"content":content,"created_at":now}

@app.put("/skills/{skill_id}")
async def update_skill(skill_id: int, request: Request):
    b = await request.json()
    with get_db() as conn:
        conn.execute("UPDATE skills SET name=?,content=?,agent=? WHERE id=?",(b.get("name","").strip(),b.get("content","").strip(),b.get("agent","shared").strip(),skill_id))
        conn.commit()
    return {"ok":True}

@app.delete("/skills/{skill_id}")
async def delete_skill(skill_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM skills WHERE id=?",(skill_id,))
        conn.commit()
    return {"ok":True}

@app.post("/chat")
async def chat(request: Request):
    body       = await request.json()
    message    = body.get("message","").strip()
    session_id = body.get("sessionId","default")
    if not message: return JSONResponse({"error":"message required"},status_code=400)

    async def generate():
        def sse(data): return f"data: {json.dumps(data)}\n\n"

        sess    = get_session(session_id)
        history = sess["messages"]
        history.append({"role":"user","content":message,"agent":"user"})
        chat_msgs = [{"role":m["role"],"content":m["content"]} for m in history if m["role"] in ("user","assistant")]

        # FAST PATH: GitHub intent — no model involved
        intent, params = parse_github_intent(message)
        if intent:
            yield sse({"type":"status","agent":"coder","status":"working"})
            yield sse({"type":"tool_call","agent":"coder","tool":intent,"params":params})
            result = await execute_intent(intent, params)
            yield sse({"type":"tool_result","agent":"coder","tool":intent,"result":result})
            yield sse({"type":"chunk","agent":"coder","chunk":result})
            history.append({"role":"assistant","content":result,"agent":"coder"})
            title = message[:60] if sess["title"]=="New conversation" else sess["title"]
            save_session(session_id, title, history)
            yield sse({"type":"status","agent":"coder","status":"done"})
            yield sse({"type":"done","sessionId":session_id,"delegations":["coder"]})
            for k in AGENT_KEYS: yield sse({"type":"status","agent":k,"status":"idle"})
            return

        # NORMAL PATH: Manager
        yield sse({"type":"status","agent":"manager","status":"thinking"})
        manager_response = ""
        async for chunk in ollama_stream(build_system("manager"), chat_msgs):
            manager_response += chunk
            yield sse({"type":"chunk","agent":"manager","chunk":chunk})

        delegations = re.findall(r'<delegate agent="(\w+)">(.*?)</delegate>', manager_response, re.DOTALL)

        if not delegations:
            history.append({"role":"assistant","content":manager_response,"agent":"manager"})
            title = message[:60] if sess["title"]=="New conversation" else sess["title"]
            save_session(session_id, title, history)
            yield sse({"type":"done","sessionId":session_id,"delegations":[]})
            for k in AGENT_KEYS: yield sse({"type":"status","agent":k,"status":"idle"})
            return

        specialist_results = {}
        for agent_key, task in delegations:
            if agent_key not in AGENTS: continue
            yield sse({"type":"status","agent":agent_key,"status":"working"})
            result = ""
            async for chunk in ollama_stream(build_system(agent_key), [{"role":"user","content":task.strip()}]):
                result += chunk
                yield sse({"type":"chunk","agent":agent_key,"chunk":chunk})
            specialist_results[agent_key] = result
            yield sse({"type":"status","agent":agent_key,"status":"done"})

        yield sse({"type":"status","agent":"manager","status":"synthesizing"})
        summary = "\n\n".join(f"[{AGENTS[k]['name']}]:\n{v}" for k,v in specialist_results.items())
        synth_msgs = chat_msgs + [{"role":"assistant","content":manager_response},{"role":"user","content":f'User asked: "{message}"\n\nResults:\n{summary}\n\nPresent clearly.'}]
        synthesis = ""
        async for chunk in ollama_stream(build_system("manager"), synth_msgs):
            synthesis += chunk
            yield sse({"type":"synthesis_chunk","agent":"manager","chunk":chunk})

        history.append({"role":"assistant","content":synthesis,"agent":"manager"})
        title = message[:60] if sess["title"]=="New conversation" else sess["title"]
        save_session(session_id, title, history)
        yield sse({"type":"done","sessionId":session_id,"delegations":[k for k,_ in delegations]})
        for k in AGENT_KEYS: yield sse({"type":"status","agent":k,"status":"idle"})

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.get("/github/repos")
async def gh_repos():
    try: return await gh("GET", "/user/repos?sort=updated&per_page=30")
    except Exception as e: return JSONResponse({"error":str(e)},status_code=500)

@app.get("/github/repos/{owner}/{repo}/contents")
async def gh_contents(owner: str, repo: str, path: str = ""):
    try: return await gh("GET", f"/repos/{owner}/{repo}/contents/{path}")
    except Exception as e: return JSONResponse({"error":str(e)},status_code=500)

@app.get("/github/repos/{owner}/{repo}/branches")
async def gh_branches(owner: str, repo: str):
    try: return await gh("GET", f"/repos/{owner}/{repo}/branches")
    except Exception as e: return JSONResponse({"error":str(e)},status_code=500)

@app.get("/github/repos/{owner}/{repo}/pulls")
async def gh_pulls(owner: str, repo: str):
    try: return await gh("GET", f"/repos/{owner}/{repo}/pulls?state=open")
    except Exception as e: return JSONResponse({"error":str(e)},status_code=500)

@app.post("/github/repos/{owner}/{repo}/commits")
async def gh_commit(owner: str, repo: str, request: Request):
    try:
        b = await request.json()
        payload = {"message":b["message"],"content":base64.b64encode(b["content"].encode()).decode(),"branch":b.get("branch","main")}
        if b.get("sha"): payload["sha"] = b["sha"]
        return await gh("PUT", f"/repos/{owner}/{repo}/contents/{b['path']}", payload)
    except Exception as e: return JSONResponse({"error":str(e)},status_code=500)

@app.post("/github/repos/{owner}/{repo}/pulls")
async def gh_create_pr(owner: str, repo: str, request: Request):
    try: return await gh("POST", f"/repos/{owner}/{repo}/pulls", await request.json())
    except Exception as e: return JSONResponse({"error":str(e)},status_code=500)

app.mount("/", StaticFiles(directory="public", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print(f"""
\U0001f680 Kal-AI running at http://localhost:{PORT}
   Model:  {OLLAMA_MODEL}
   Ollama: {OLLAMA_HOST}
   GitHub: {'connected' if GITHUB_TOKEN else 'no token'}

   Supported commands:
   Read:  list repos, show files in X repo, read FILE in X repo, list branches in X repo
   Write: create branch NAME in X repo, merge BRANCH into BRANCH in X repo,
          create PR from BRANCH to BRANCH in X repo, merge PR #N in X repo

   Make sure Ollama is running: ollama serve
""")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
