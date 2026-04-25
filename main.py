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
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
PORT         = int(os.getenv("PORT", 3000))

# ---------------------------------------------------------------------------
# GitHub API helper
# ---------------------------------------------------------------------------
async def gh(method: str, endpoint: str, body: dict = None) -> dict:
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "kal-ai/1.0",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method, f"https://api.github.com{endpoint}", headers=headers, json=body
        )
        if resp.status_code == 204:
            return {}
        data = resp.json()
        if resp.status_code >= 400:
            raise Exception(data.get("message", f"GitHub error {resp.status_code}"))
        return data

# ---------------------------------------------------------------------------
# Tool executor — agents call these by outputting <tool> tags
# ---------------------------------------------------------------------------
async def run_tool(name: str, params: dict) -> str:
    try:
        if name == "list_repos":
            data = await gh("GET", "/user/repos?sort=updated&per_page=50")
            return json.dumps([{
                "name": r["name"], "full_name": r["full_name"],
                "default_branch": r["default_branch"],
                "description": r.get("description", ""),
                "private": r["private"]
            } for r in data], indent=2)

        elif name == "list_branches":
            owner, repo = params["owner"], params["repo"]
            data = await gh("GET", f"/repos/{owner}/{repo}/branches")
            return json.dumps([b["name"] for b in data])

        elif name == "list_files":
            owner, repo = params["owner"], params["repo"]
            path   = params.get("path", "").lstrip("/")
            branch = params.get("branch", "")
            ep = f"/repos/{owner}/{repo}/contents/{path}"
            if branch: ep += f"?ref={branch}"
            data = await gh("GET", ep)
            if isinstance(data, list):
                return json.dumps([{"name": f["name"], "type": f["type"], "path": f["path"]} for f in data], indent=2)
            return json.dumps(data)

        elif name == "read_file":
            owner, repo, path = params["owner"], params["repo"], params["path"]
            branch = params.get("branch", "")
            ep = f"/repos/{owner}/{repo}/contents/{path}"
            if branch: ep += f"?ref={branch}"
            data = await gh("GET", ep)
            content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8", errors="replace")
            return json.dumps({"path": path, "sha": data["sha"], "content": content})

        elif name == "commit_file":
            owner, repo = params["owner"], params["repo"]
            path, content = params["path"], params["content"]
            message = params["message"]
            branch  = params.get("branch", "main")
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
            return json.dumps({"ok": True, "commit": result.get("commit", {}).get("sha", ""), "path": path, "branch": branch})

        elif name == "create_branch":
            owner, repo, branch = params["owner"], params["repo"], params["branch"]
            from_branch = params.get("from_branch", "main")
            ref_data = await gh("GET", f"/repos/{owner}/{repo}/git/ref/heads/{from_branch}")
            sha = ref_data["object"]["sha"]
            await gh("POST", f"/repos/{owner}/{repo}/git/refs",
                     {"ref": f"refs/heads/{branch}", "sha": sha})
            return json.dumps({"ok": True, "branch": branch, "from": from_branch})

        elif name == "merge_branch":
            owner, repo = params["owner"], params["repo"]
            base, head = params["base"], params["head"]
            message = params.get("message", f"Merge {head} into {base}")
            result = await gh("POST", f"/repos/{owner}/{repo}/merges",
                              {"base": base, "head": head, "commit_message": message})
            return json.dumps({"ok": True, "sha": result.get("sha", "")})

        elif name == "create_pr":
            owner, repo = params["owner"], params["repo"]
            result = await gh("POST", f"/repos/{owner}/{repo}/pulls", {
                "title": params["title"],
                "head":  params["head"],
                "base":  params.get("base", "main"),
                "body":  params.get("body", ""),
            })
            return json.dumps({"ok": True, "number": result["number"], "url": result["html_url"]})

        elif name == "merge_pr":
            owner, repo, number = params["owner"], params["repo"], params["number"]
            method = params.get("method", "squash")  # merge | squash | rebase
            result = await gh("PUT", f"/repos/{owner}/{repo}/pulls/{number}/merge",
                              {"merge_method": method,
                               "commit_title": params.get("title", ""),
                               "commit_message": params.get("message", "")})
            return json.dumps({"ok": True, "merged": result.get("merged", False), "sha": result.get("sha", "")})

        elif name == "list_prs":
            owner, repo = params["owner"], params["repo"]
            state = params.get("state", "open")
            data = await gh("GET", f"/repos/{owner}/{repo}/pulls?state={state}&per_page=20")
            return json.dumps([{"number": p["number"], "title": p["title"],
                                "head": p["head"]["ref"], "base": p["base"]["ref"],
                                "state": p["state"]} for p in data], indent=2)

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})

# ---------------------------------------------------------------------------
# Tool system prompt injected into agents that can use GitHub
# ---------------------------------------------------------------------------
GITHUB_TOOLS_PROMPT = """
## GitHub Tools
You have direct access to GitHub. Use these tools to read, write, commit, push, branch, and merge.
Output tool calls in this EXACT format — one per line, nothing else on that line:
<tool name="TOOL_NAME">{"param": "value"}</tool>

Tools available:

- list_repos — list all your repos
  <tool name="list_repos">{}</tool>

- list_branches — list branches
  <tool name="list_branches">{"owner": "kale87", "repo": "myapp"}</tool>

- list_files — list files in a directory
  <tool name="list_files">{"owner": "kale87", "repo": "myapp", "path": "src", "branch": "main"}</tool>

- read_file — read file contents (also returns SHA needed for commit_file)
  <tool name="read_file">{"owner": "kale87", "repo": "myapp", "path": "src/App.js", "branch": "main"}</tool>

- commit_file — create or update a file (commits + pushes in one step)
  <tool name="commit_file">{"owner": "kale87", "repo": "myapp", "path": "src/App.js", "content": "...full file content...", "message": "fix: update App.js", "branch": "main"}</tool>

- create_branch — create a new branch
  <tool name="create_branch">{"owner": "kale87", "repo": "myapp", "branch": "fix/my-fix", "from_branch": "main"}</tool>

- merge_branch — merge one branch into another directly
  <tool name="merge_branch">{"owner": "kale87", "repo": "myapp", "base": "main", "head": "fix/my-fix"}</tool>

- create_pr — open a pull request
  <tool name="create_pr">{"owner": "kale87", "repo": "myapp", "title": "fix: my fix", "head": "fix/my-fix", "base": "main", "body": "Description"}</tool>

- merge_pr — merge an open pull request
  <tool name="merge_pr">{"owner": "kale87", "repo": "myapp", "number": 5, "method": "squash"}</tool>

- list_prs — list pull requests
  <tool name="list_prs">{"owner": "kale87", "repo": "myapp", "state": "open"}</tool>

RULES:
- Always read_file before commit_file so you have the latest content.
- Always explain what you are doing before each tool call.
- After the tool result, explain what happened and continue.
- For tasks involving multiple files, use multiple tool calls in sequence.
- commit_file both commits AND pushes — there is no separate push step.
"""

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
AGENTS = {
    "manager": {
        "name": "Manager", "emoji": "🎯", "color": "#6366f1",
        "tools": False,
        "system": """You are the Manager agent of a multi-agent AI system called Kal-AI.

Your job:
1. Read the user's message carefully.
2. If it is a SIMPLE QUESTION (asking for information, explanation, opinion) — answer it directly yourself. DO NOT delegate.
3. If it is a TASK (build something, fix something, commit code, research then write, etc.) — break it into subtasks and delegate to the right specialists.

To delegate use this EXACT format:
<delegate agent="coder">specific task description</delegate>
<delegate agent="researcher">specific task description</delegate>
<delegate agent="writer">specific task description</delegate>

Specialists:
- coder: writing/fixing/reviewing code, reading and committing to GitHub repos
- researcher: research, analysis, summarizing information
- writer: writing content, documentation, commit messages, PR descriptions

For tasks, you can delegate to multiple agents at once. After they respond, synthesize into a final clear answer.
For questions, just answer directly — no delegation needed.""",
    },
    "coder": {
        "name": "Coder", "emoji": "💻", "color": "#10b981",
        "tools": True,
        "system": """You are the Coder agent of Kal-AI. You write clean code, fix bugs, review code, and work directly with GitHub repos.

When asked to read, modify, commit, push, branch, or merge code — USE YOUR GITHUB TOOLS. Do not just describe what to do, actually do it.

Always:
- Read files before editing them
- Use meaningful commit messages
- Explain what you changed and why
- Work on the correct branch""",
    },
    "researcher": {
        "name": "Researcher", "emoji": "🔍", "color": "#f59e0b",
        "tools": True,
        "system": """You are the Researcher agent of Kal-AI. You gather information, analyze codebases, and provide context.

You can read GitHub repos to understand a codebase before the Coder makes changes. Use your tools to explore repos and files.""",
    },
    "writer": {
        "name": "Writer", "emoji": "✍️", "color": "#ec4899",
        "tools": False,
        "system": "You are the Writer agent of Kal-AI. You write clear content, documentation, commit messages, and PR descriptions. Be concise and precise.",
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
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL DEFAULT 'New conversation',
            messages      TEXT NOT NULL DEFAULT '[]',
            last_accessed INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skills (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            agent      TEXT NOT NULL DEFAULT 'shared',
            content    TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)
    conn.commit()

def get_session(sid: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    return {"title": row["title"], "messages": json.loads(row["messages"])} if row else {"title": "New conversation", "messages": []}

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
            "SELECT name, content FROM skills WHERE agent='shared' OR agent=? ORDER BY created_at",
            (agent_key,)
        ).fetchall()
    if not rows:
        return ""
    parts = "\n\n".join(f"### Skill: {r['name']}\n{r['content']}" for r in rows)
    return f"\n\n---\n## Your Skills\n{parts}"

def build_system(agent_key: str) -> str:
    agent = AGENTS[agent_key]
    base  = agent["system"]
    tools = GITHUB_TOOLS_PROMPT if agent["tools"] else ""
    skills = get_skills(agent_key)
    return base + tools + skills

# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
async def ollama_stream(system: str, messages: list) -> AsyncGenerator[str, None]:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}] + messages,
        "stream": True,
    }
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
# Agent runner — handles tool calls in a loop
# ---------------------------------------------------------------------------
TOOL_RE = re.compile(r'<tool name="([\w]+)">({.*?})</tool>', re.DOTALL)

async def run_agent(agent_key: str, messages: list, on_chunk, on_tool_call, on_tool_result) -> str:
    """
    Run an agent with tool-calling loop.
    Calls on_chunk(text), on_tool_call(name, params), on_tool_result(name, result) as callbacks.
    Returns the final full response text.
    """
    system   = build_system(agent_key)
    history  = list(messages)
    full_out = ""

    for _ in range(10):  # max 10 tool-call rounds
        response = ""
        async for chunk in ollama_stream(system, history):
            response += chunk
            # Stream visible text (skip tool call lines)
            if "<tool" not in chunk:
                on_chunk(chunk)

        full_out += response

        # Find tool calls in the response
        tool_calls = TOOL_RE.findall(response)
        if not tool_calls:
            break  # no more tool calls — done

        # Add assistant message to history
        history.append({"role": "assistant", "content": response})

        # Execute each tool and collect results
        tool_results = ""
        for tool_name, params_str in tool_calls:
            try:
                params = json.loads(params_str)
            except Exception:
                params = {}
            on_tool_call(tool_name, params)
            result = await run_tool(tool_name, params)
            on_tool_result(tool_name, result)
            tool_results += f"[TOOL RESULT: {tool_name}]\n{result}\n\n"

        # Feed results back as user message
        history.append({"role": "user", "content": tool_results.strip()})

    return full_out

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

# Sessions
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

# Skills
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
# Chat — smart routing: question → manager answers, task → all agents work
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

        # Step 1: Manager decides — answer directly or delegate
        manager_response = ""

        def on_chunk(c):
            nonlocal manager_response
            manager_response += c

        def noop_tool(*a): pass

        # Stream manager response
        system = build_system("manager")
        async for chunk in ollama_stream(system, chat_msgs):
            manager_response += chunk
            yield sse({"type": "chunk", "agent": "manager", "chunk": chunk})

        # Step 2: Parse delegations
        delegations = re.findall(r'<delegate agent="(\w+)">(.*?)</delegate>',
                                  manager_response, re.DOTALL)

        # Step 3: If no delegations — manager answered directly, we're done
        if not delegations:
            history.append({"role": "assistant", "content": manager_response, "agent": "manager"})
            title = message[:60] if sess["title"] == "New conversation" else sess["title"]
            save_session(session_id, title, history)
            yield sse({"type": "done", "sessionId": session_id, "delegations": []})
            for k in AGENT_KEYS:
                yield sse({"type": "status", "agent": k, "status": "idle"})
            return

        # Step 4: Run each specialist with tool support
        specialist_results = {}
        for agent_key, task in delegations:
            if agent_key not in AGENTS:
                continue
            yield sse({"type": "status", "agent": agent_key, "status": "working"})

            agent_chunks = []
            tool_events  = []

            def on_agent_chunk(c, ak=agent_key):
                agent_chunks.append(c)

            def on_tool_call(name, params, ak=agent_key):
                tool_events.append(("call", name, params))

            def on_tool_result(name, result, ak=agent_key):
                tool_events.append(("result", name, result))

            agent_msgs = [{"role": "user", "content": task.strip()}]
            result = await run_agent(agent_key, agent_msgs, on_agent_chunk, on_tool_call, on_tool_result)

            # Stream agent chunks
            for c in agent_chunks:
                yield sse({"type": "chunk", "agent": agent_key, "chunk": c})

            # Stream tool events
            for ev in tool_events:
                if ev[0] == "call":
                    yield sse({"type": "tool_call", "agent": agent_key, "tool": ev[1], "params": ev[2]})
                else:
                    try:
                        parsed = json.loads(ev[2])
                    except Exception:
                        parsed = ev[2]
                    yield sse({"type": "tool_result", "agent": agent_key, "tool": ev[1], "result": parsed})

            specialist_results[agent_key] = result
            yield sse({"type": "status", "agent": agent_key, "status": "done"})

        # Step 5: Manager synthesizes all results
        yield sse({"type": "status", "agent": "manager", "status": "synthesizing"})
        summary = "\n\n".join(f"[{AGENTS[k]['name']}]:\n{v}" for k, v in specialist_results.items())
        synth_prompt = (
            f'The user asked: "{message}"\n\n'
            f'Here are the specialist results:\n{summary}\n\n'
            f'Give the user a single clear well-organized final answer. '
            f'If agents made GitHub commits or changes, summarize exactly what was done.'
        )
        synth_msgs = chat_msgs + [
            {"role": "assistant", "content": manager_response},
            {"role": "user",      "content": synth_prompt},
        ]
        synthesis = ""
        async for chunk in ollama_stream(build_system("manager"), synth_msgs):
            synthesis += chunk
            yield sse({"type": "synthesis_chunk", "agent": "manager", "chunk": chunk})

        final = synthesis
        history.append({"role": "assistant", "content": final, "agent": "manager"})
        title = message[:60] if sess["title"] == "New conversation" else sess["title"]
        save_session(session_id, title, history)

        yield sse({"type": "done", "sessionId": session_id,
                   "delegations": [k for k, _ in delegations]})
        for k in AGENT_KEYS:
            yield sse({"type": "status", "agent": k, "status": "idle"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

# ---------------------------------------------------------------------------
# GitHub panel API (for the UI panel — separate from agent tools)
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

@app.post("/github/repos/{owner}/{repo}/pulls/{number}/reviews")
async def gh_review(owner: str, repo: str, number: int, request: Request):
    try: return await gh("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", await request.json())
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
