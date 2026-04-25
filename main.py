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

# ---------------------------------------------------------------------------
# GitHub tool definitions — sent to Ollama as native tools
# ---------------------------------------------------------------------------
GITHUB_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_repos",
            "description": "List all GitHub repositories for the authenticated user.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_branches",
            "description": "List all branches in a GitHub repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner (username)"},
                    "repo":  {"type": "string", "description": "Repository name"},
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories in a repository path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner":  {"type": "string"},
                    "repo":   {"type": "string"},
                    "path":   {"type": "string", "description": "Directory path, empty for root"},
                    "branch": {"type": "string", "description": "Branch name, defaults to main"},
                },
                "required": ["owner", "repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in a GitHub repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner":  {"type": "string"},
                    "repo":   {"type": "string"},
                    "path":   {"type": "string", "description": "Full file path"},
                    "branch": {"type": "string", "description": "Branch name"},
                },
                "required": ["owner", "repo", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commit_file",
            "description": "Create or update a file in a GitHub repository with a commit. This also pushes the change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner":   {"type": "string"},
                    "repo":    {"type": "string"},
                    "path":    {"type": "string", "description": "Full file path"},
                    "content": {"type": "string", "description": "Full file content"},
                    "message": {"type": "string", "description": "Commit message"},
                    "branch":  {"type": "string", "description": "Branch to commit to, defaults to main"},
                },
                "required": ["owner", "repo", "path", "content", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_branch",
            "description": "Create a new branch in a GitHub repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner":       {"type": "string"},
                    "repo":        {"type": "string"},
                    "branch":      {"type": "string", "description": "New branch name"},
                    "from_branch": {"type": "string", "description": "Source branch, defaults to main"},
                },
                "required": ["owner", "repo", "branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "merge_branch",
            "description": "Merge one branch into another in a GitHub repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner":   {"type": "string"},
                    "repo":    {"type": "string"},
                    "base":    {"type": "string", "description": "Branch to merge into"},
                    "head":    {"type": "string", "description": "Branch to merge from"},
                    "message": {"type": "string", "description": "Merge commit message"},
                },
                "required": ["owner", "repo", "base", "head"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_pr",
            "description": "Open a pull request in a GitHub repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo":  {"type": "string"},
                    "title": {"type": "string"},
                    "head":  {"type": "string", "description": "Branch to merge from"},
                    "base":  {"type": "string", "description": "Branch to merge into, defaults to main"},
                    "body":  {"type": "string", "description": "PR description"},
                },
                "required": ["owner", "repo", "title", "head"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "merge_pr",
            "description": "Merge an open pull request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner":   {"type": "string"},
                    "repo":    {"type": "string"},
                    "number":  {"type": "integer", "description": "PR number"},
                    "method":  {"type": "string", "description": "merge, squash, or rebase"},
                },
                "required": ["owner", "repo", "number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_prs",
            "description": "List pull requests in a repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo":  {"type": "string"},
                    "state": {"type": "string", "description": "open, closed, or all"},
                },
                "required": ["owner", "repo"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------
async def gh(method: str, endpoint: str, body: dict = None) -> dict:
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "kal-ai/1.0",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(method, f"https://api.github.com{endpoint}", headers=headers, json=body)
        if resp.status_code == 204:
            return {}
        data = resp.json()
        if resp.status_code >= 400:
            raise Exception(data.get("message", f"GitHub error {resp.status_code}"))
        return data

# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------
async def run_tool(name: str, params: dict) -> str:
    try:
        if name == "list_repos":
            data = await gh("GET", "/user/repos?sort=updated&per_page=50")
            return json.dumps([{"name": r["name"], "full_name": r["full_name"],
                                "default_branch": r["default_branch"],
                                "description": r.get("description", ""),
                                "private": r["private"]} for r in data], indent=2)

        elif name == "list_branches":
            data = await gh("GET", f"/repos/{params['owner']}/{params['repo']}/branches")
            return json.dumps([b["name"] for b in data])

        elif name == "list_files":
            path   = params.get("path", "").lstrip("/")
            branch = params.get("branch", "")
            ep = f"/repos/{params['owner']}/{params['repo']}/contents/{path}"
            if branch: ep += f"?ref={branch}"
            data = await gh("GET", ep)
            if isinstance(data, list):
                return json.dumps([{"name": f["name"], "type": f["type"], "path": f["path"]} for f in data], indent=2)
            return json.dumps(data)

        elif name == "read_file":
            branch = params.get("branch", "")
            ep = f"/repos/{params['owner']}/{params['repo']}/contents/{params['path']}"
            if branch: ep += f"?ref={branch}"
            data = await gh("GET", ep)
            content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
            return json.dumps({"path": params["path"], "sha": data["sha"], "content": content})

        elif name == "commit_file":
            branch = params.get("branch", "main")
            body: dict = {
                "message": params["message"],
                "content": base64.b64encode(params["content"].encode()).decode(),
                "branch":  branch,
            }
            try:
                existing = await gh("GET", f"/repos/{params['owner']}/{params['repo']}/contents/{params['path']}?ref={branch}")
                body["sha"] = existing["sha"]
            except Exception:
                pass
            result = await gh("PUT", f"/repos/{params['owner']}/{params['repo']}/contents/{params['path']}", body)
            return json.dumps({"ok": True, "commit": result.get("commit", {}).get("sha", ""), "path": params["path"], "branch": branch})

        elif name == "create_branch":
            from_branch = params.get("from_branch", "main")
            ref_data = await gh("GET", f"/repos/{params['owner']}/{params['repo']}/git/ref/heads/{from_branch}")
            sha = ref_data["object"]["sha"]
            await gh("POST", f"/repos/{params['owner']}/{params['repo']}/git/refs",
                     {"ref": f"refs/heads/{params['branch']}", "sha": sha})
            return json.dumps({"ok": True, "branch": params["branch"], "from": from_branch})

        elif name == "merge_branch":
            result = await gh("POST", f"/repos/{params['owner']}/{params['repo']}/merges", {
                "base": params["base"], "head": params["head"],
                "commit_message": params.get("message", f"Merge {params['head']} into {params['base']}")
            })
            return json.dumps({"ok": True, "sha": result.get("sha", "")})

        elif name == "create_pr":
            result = await gh("POST", f"/repos/{params['owner']}/{params['repo']}/pulls", {
                "title": params["title"], "head": params["head"],
                "base":  params.get("base", "main"), "body": params.get("body", ""),
            })
            return json.dumps({"ok": True, "number": result["number"], "url": result["html_url"]})

        elif name == "merge_pr":
            result = await gh("PUT", f"/repos/{params['owner']}/{params['repo']}/pulls/{params['number']}/merge", {
                "merge_method": params.get("method", "squash")
            })
            return json.dumps({"ok": True, "merged": result.get("merged", False), "sha": result.get("sha", "")})

        elif name == "list_prs":
            state = params.get("state", "open")
            data = await gh("GET", f"/repos/{params['owner']}/{params['repo']}/pulls?state={state}&per_page=20")
            return json.dumps([{"number": p["number"], "title": p["title"],
                                "head": p["head"]["ref"], "base": p["base"]["ref"],
                                "state": p["state"]} for p in data], indent=2)
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)})

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
AGENTS = {
    "manager": {
        "name": "Manager", "emoji": "🎯", "color": "#6366f1", "tools": False,
        "system": """You are the Manager agent of Kal-AI, a multi-agent AI system.

Rules:
1. If the user asks a QUESTION (needs information or explanation) — answer it directly. Do NOT delegate.
2. If the user gives a TASK (build, fix, commit, research+write, etc.) — delegate to specialists.

To delegate:
<delegate agent="coder">task description</delegate>
<delegate agent="researcher">task description</delegate>
<delegate agent="writer">task description</delegate>

Specialists:
- coder: code, GitHub operations (read/commit/push/branch/merge), debugging
- researcher: research, analysis, reading codebases
- writer: documentation, commit messages, content

After specialists finish, synthesize a clear final answer.""",
    },
    "coder": {
        "name": "Coder", "emoji": "💻", "color": "#10b981", "tools": True,
        "system": """You are the Coder agent of Kal-AI. You write code, fix bugs, and work directly with GitHub.

You have GitHub tools available. When asked to read, modify, commit, push, branch, or merge — USE YOUR TOOLS. Do not describe what to do, actually do it.
- Always read a file before editing it
- Use clear commit messages
- Work on the correct branch""",
    },
    "researcher": {
        "name": "Researcher", "emoji": "🔍", "color": "#f59e0b", "tools": True,
        "system": """You are the Researcher agent of Kal-AI. You gather information and analyze codebases.

You have GitHub tools available. Use them to explore repos and read files when needed.""",
    },
    "writer": {
        "name": "Writer", "emoji": "✍️", "color": "#ec4899", "tools": False,
        "system": "You are the Writer agent of Kal-AI. You write clear documentation, commit messages, and content.",
    },
}

AGENT_KEYS = list(AGENTS.keys())

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH  = DATA_DIR / "sessions.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

with get_db() as conn:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'New conversation',
            messages TEXT NOT NULL DEFAULT '[]', last_accessed INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            agent TEXT NOT NULL DEFAULT 'shared', content TEXT NOT NULL, created_at INTEGER NOT NULL
        )
    """)
    conn.commit()

def get_session(sid: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return {"title": row["title"], "messages": json.loads(row["messages"])} if row \
        else {"title": "New conversation", "messages": []}

def save_session(sid: str, title: str, messages: list):
    now = int(datetime.now().timestamp() * 1000)
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sessions (id,title,messages,last_accessed) VALUES (?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,messages=excluded.messages,last_accessed=excluded.last_accessed
        """, (sid, title, json.dumps(messages), now))
        conn.commit()

def get_skills(agent_key: str) -> str:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name,content FROM skills WHERE agent='shared' OR agent=? ORDER BY created_at",
            (agent_key,)
        ).fetchall()
    if not rows: return ""
    parts = "\n\n".join(f"### Skill: {r['name']}\n{r['content']}" for r in rows)
    return f"\n\n---\n## Your Skills\n{parts}"

def build_system(agent_key: str) -> str:
    return AGENTS[agent_key]["system"] + get_skills(agent_key)

# ---------------------------------------------------------------------------
# Ollama — non-streaming call that supports native tool use
# ---------------------------------------------------------------------------
async def ollama_call(system: str, messages: list, tools: list = None) -> dict:
    """Single Ollama call, returns full message object."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}] + messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{OLLAMA_HOST}/api/chat", json=payload)
        data = resp.json()
    return data.get("message", {"role": "assistant", "content": ""})

async def ollama_stream(system: str, messages: list) -> AsyncGenerator[str, None]:
    """Streaming call — used for manager and writer (no tools)."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}] + messages,
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", f"{OLLAMA_HOST}/api/chat", json=payload) as resp:
            async for line in resp.aiter_lines():
                if not line: continue
                try:
                    chunk = json.loads(line).get("message", {}).get("content", "")
                    if chunk: yield chunk
                except Exception:
                    pass

# ---------------------------------------------------------------------------
# Agent runner with native tool loop
# ---------------------------------------------------------------------------
async def run_agent_with_tools(agent_key: str, task: str, on_chunk, on_tool_call, on_tool_result) -> str:
    """
    Run an agent using Ollama native function calling.
    Loops until the model stops calling tools.
    """
    system   = build_system(agent_key)
    tools    = GITHUB_TOOLS if AGENTS[agent_key]["tools"] else None
    messages = [{"role": "user", "content": task}]
    full_text = ""

    for _ in range(10):  # max 10 tool-call rounds
        msg = await ollama_call(system, messages, tools)
        tool_calls = msg.get("tool_calls", [])

        if not tool_calls:
            # No tool calls — stream the text response
            content = msg.get("content", "")
            full_text += content
            on_chunk(content)
            break

        # Has tool calls — execute them
        messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": tool_calls})

        for tc in tool_calls:
            fn   = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try: args = json.loads(args)
                except Exception: args = {}

            on_tool_call(name, args)
            result = await run_tool(name, args)
            on_tool_result(name, result)

            messages.append({"role": "tool", "content": result})

    return full_text

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Kal-AI")

@app.get("/health")
async def health():
    return {"ok": True, "model": OLLAMA_MODEL, "ollama": OLLAMA_HOST, "agents": AGENT_KEYS}

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
        rows = conn.execute("""
            SELECT id,title,messages,last_accessed FROM sessions
            WHERE id LIKE 'ui-%' ORDER BY last_accessed DESC LIMIT 50
        """).fetchall()
    return [{"id": r["id"], "title": r["title"],
             "count": len(json.loads(r["messages"])), "lastAccessed": r["last_accessed"]}
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
            (agent,) if agent else ()
        ).fetchall()
    return [dict(r) for r in rows]

@app.post("/skills")
async def create_skill(request: Request):
    b = await request.json()
    name, agent, content = b.get("name","").strip(), b.get("agent","shared").strip(), b.get("content","").strip()
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
                     (b.get("name","").strip(), b.get("content","").strip(), b.get("agent","shared").strip(), skill_id))
        conn.commit()
    return {"ok": True}

@app.delete("/skills/{skill_id}")
async def delete_skill(skill_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        conn.commit()
    return {"ok": True}

# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@app.post("/chat")
async def chat(request: Request):
    body       = await request.json()
    message    = body.get("message", "").strip()
    session_id = body.get("sessionId", "default")
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)

    async def generate():
        def sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        sess    = get_session(session_id)
        history = sess["messages"]
        history.append({"role": "user", "content": message, "agent": "user"})
        yield sse({"type": "status", "agent": "manager", "status": "thinking"})

        chat_msgs = [{"role": m["role"], "content": m["content"]}
                     for m in history if m["role"] in ("user", "assistant")]

        # Step 1: Manager decides — stream response
        manager_response = ""
        async for chunk in ollama_stream(build_system("manager"), chat_msgs):
            manager_response += chunk
            yield sse({"type": "chunk", "agent": "manager", "chunk": chunk})

        # Step 2: Parse delegations
        delegations = re.findall(r'<delegate agent="(\w+)">(.*?)</delegate>',
                                  manager_response, re.DOTALL)

        # No delegations — manager answered directly
        if not delegations:
            history.append({"role": "assistant", "content": manager_response, "agent": "manager"})
            title = message[:60] if sess["title"] == "New conversation" else sess["title"]
            save_session(session_id, title, history)
            yield sse({"type": "done", "sessionId": session_id, "delegations": []})
            for k in AGENT_KEYS:
                yield sse({"type": "status", "agent": k, "status": "idle"})
            return

        # Step 3: Run specialists with native tool calling
        specialist_results = {}
        for agent_key, task in delegations:
            if agent_key not in AGENTS:
                continue
            yield sse({"type": "status", "agent": agent_key, "status": "working"})

            chunks, tool_events = [], []

            def on_chunk(c, ak=agent_key): chunks.append(c)
            def on_tool_call(name, params, ak=agent_key): tool_events.append(("call", name, params))
            def on_tool_result(name, result, ak=agent_key): tool_events.append(("result", name, result))

            result = await run_agent_with_tools(agent_key, task.strip(), on_chunk, on_tool_call, on_tool_result)

            for c in chunks:
                yield sse({"type": "chunk", "agent": agent_key, "chunk": c})
            for ev in tool_events:
                if ev[0] == "call":
                    yield sse({"type": "tool_call", "agent": agent_key, "tool": ev[1], "params": ev[2]})
                else:
                    try: parsed = json.loads(ev[2])
                    except Exception: parsed = ev[2]
                    yield sse({"type": "tool_result", "agent": agent_key, "tool": ev[1], "result": parsed})

            specialist_results[agent_key] = result
            yield sse({"type": "status", "agent": agent_key, "status": "done"})

        # Step 4: Manager synthesizes
        yield sse({"type": "status", "agent": "manager", "status": "synthesizing"})
        summary = "\n\n".join(f"[{AGENTS[k]['name']}]:\n{v}" for k, v in specialist_results.items())
        synth_msgs = chat_msgs + [
            {"role": "assistant", "content": manager_response},
            {"role": "user", "content": (
                f'The user asked: "{message}"\n\nSpecialist results:\n{summary}\n\n'
                f'Give a single clear final answer. If GitHub actions were taken, summarize exactly what was done.'
            )},
        ]
        synthesis = ""
        async for chunk in ollama_stream(build_system("manager"), synth_msgs):
            synthesis += chunk
            yield sse({"type": "synthesis_chunk", "agent": "manager", "chunk": chunk})

        history.append({"role": "assistant", "content": synthesis, "agent": "manager"})
        title = message[:60] if sess["title"] == "New conversation" else sess["title"]
        save_session(session_id, title, history)
        yield sse({"type": "done", "sessionId": session_id, "delegations": [k for k, _ in delegations]})
        for k in AGENT_KEYS:
            yield sse({"type": "status", "agent": k, "status": "idle"})

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ---------------------------------------------------------------------------
# GitHub panel API (for the UI panel)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory="public", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print(f"""
\U0001f680 Kal-AI running at http://localhost:{PORT}
   Model:  {OLLAMA_MODEL}
   Ollama: {OLLAMA_HOST}
   GitHub: {'connected' if GITHUB_TOKEN else 'no token — add GITHUB_TOKEN to .env'}

   Agents with GitHub tools: coder, researcher
   Make sure Ollama is running: ollama serve
""")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
