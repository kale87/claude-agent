import os
import json
import sqlite3
import asyncio
import re
import httpx
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OLLAMA_HOST  = os.getenv("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
PORT         = int(os.getenv("PORT", 3000))

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
AGENTS = {
    "manager": {
        "name": "Manager", "emoji": "🎯", "color": "#6366f1",
        "system": """You are the Manager agent in a multi-agent AI system.
Your role:
1. Understand the user's request carefully.
2. For simple questions, answer directly.
3. For complex tasks, delegate to specialists using EXACTLY this format:
   <delegate agent=\"coder\">specific task</delegate>
   <delegate agent=\"researcher\">specific task</delegate>
   <delegate agent=\"writer\">specific task</delegate>

Available specialists:
- coder: code writing, debugging, code review, technical implementation
- researcher: research, summarizing, analysis, finding information
- writer: writing content, documentation, editing, commit messages

Be concise in your planning. Delegate clearly.""",
    },
    "coder": {
        "name": "Coder", "emoji": "💻", "color": "#10b981",
        "system": "You are the Coder agent. Specialize in writing clean well-documented code, debugging, code review, and technical explanations. Always use code blocks. Be precise and thorough.",
    },
    "researcher": {
        "name": "Researcher", "emoji": "🔍", "color": "#f59e0b",
        "system": "You are the Researcher agent. Specialize in gathering and synthesizing information, summarizing topics clearly, providing relevant context, and structured analysis.",
    },
    "writer": {
        "name": "Writer", "emoji": "✍️", "color": "#ec4899",
        "system": "You are the Writer agent. Specialize in writing clear engaging content, technical documentation, commit messages, PR descriptions, and editing text. Match tone and style to context.",
    },
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
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            agent       TEXT NOT NULL DEFAULT 'shared',
            content     TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        )
    """)
    conn.commit()

# ---------------------------------------------------------------------------
# Skills helpers
# ---------------------------------------------------------------------------
def get_skills_for_agent(agent_key: str) -> list:
    """Return shared skills + agent-specific skills."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM skills WHERE agent = 'shared' OR agent = ? ORDER BY created_at ASC",
            (agent_key,)
        ).fetchall()
    return [dict(r) for r in rows]

def build_system_prompt(agent_key: str) -> str:
    """Inject skills into agent system prompt."""
    base = AGENTS[agent_key]["system"]
    skills = get_skills_for_agent(agent_key)
    if not skills:
        return base
    skills_text = "\n\n".join(
        f"### Skill: {s['name']}\n{s['content']}" for s in skills
    )
    return f"{base}\n\n---\n## Your Skills\n{skills_text}"

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def get_session(session_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row:
        return {"title": row["title"], "messages": json.loads(row["messages"])}
    return {"title": "New conversation", "messages": []}

def save_session(session_id: str, title: str, messages: list):
    now = int(datetime.now().timestamp() * 1000)
    with get_db() as conn:
        conn.execute("""
            INSERT INTO sessions (id, title, messages, last_accessed) VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, messages=excluded.messages, last_accessed=excluded.last_accessed
        """, (session_id, title, json.dumps(messages), now))
        conn.commit()

# ---------------------------------------------------------------------------
# Ollama streaming
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
                if line:
                    try:
                        data = json.loads(line)
                        chunk = data.get("message", {}).get("content", "")
                        if chunk:
                            yield chunk
                    except json.JSONDecodeError:
                        pass

# ---------------------------------------------------------------------------
# GitHub helper
# ---------------------------------------------------------------------------
async def github_request(method: str, endpoint: str, body: dict = None):
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
            resp = await client.get(f"{OLLAMA_HOST}/api/tags")
            data = resp.json()
        models = [m["name"] for m in data.get("models", [])]
        has_model = any(m.startswith(OLLAMA_MODEL.split(":")[0]) for m in models)
        return {"running": True, "models": models, "hasModel": has_model, "currentModel": OLLAMA_MODEL}
    except Exception as e:
        return {"running": False, "error": str(e)}

# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
@app.get("/sessions")
async def list_sessions():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, title, messages, last_accessed FROM sessions
            WHERE id LIKE 'ui-%' ORDER BY last_accessed DESC LIMIT 50
        """).fetchall()
    return [{"id": r["id"], "title": r["title"], "count": len(json.loads(r["messages"])), "lastAccessed": r["last_accessed"]}
            for r in rows if json.loads(r["messages"])]

@app.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    return get_session(session_id)["messages"]

@app.post("/sessions/{session_id}/clear")
async def clear_session(session_id: str):
    save_session(session_id, "New conversation", [])
    return {"ok": True}

# ---------------------------------------------------------------------------
# Skills API
# ---------------------------------------------------------------------------
@app.get("/skills")
async def list_skills(agent: Optional[str] = None):
    with get_db() as conn:
        if agent:
            rows = conn.execute(
                "SELECT * FROM skills WHERE agent = ? OR agent = 'shared' ORDER BY created_at ASC",
                (agent,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM skills ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]

@app.post("/skills")
async def create_skill(request: Request):
    body = await request.json()
    name    = body.get("name", "").strip()
    agent   = body.get("agent", "shared").strip()
    content = body.get("content", "").strip()
    if not name or not content:
        return JSONResponse({"error": "name and content are required"}, status_code=400)
    if agent not in AGENT_KEYS and agent != "shared":
        return JSONResponse({"error": f"agent must be one of: shared, {', '.join(AGENT_KEYS)}"}, status_code=400)
    now = int(datetime.now().timestamp() * 1000)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO skills (name, agent, content, created_at) VALUES (?, ?, ?, ?)",
            (name, agent, content, now)
        )
        conn.commit()
        skill_id = cur.lastrowid
    return {"id": skill_id, "name": name, "agent": agent, "content": content, "created_at": now}

@app.put("/skills/{skill_id}")
async def update_skill(skill_id: int, request: Request):
    body = await request.json()
    name    = body.get("name", "").strip()
    content = body.get("content", "").strip()
    agent   = body.get("agent", "shared").strip()
    if not name or not content:
        return JSONResponse({"error": "name and content are required"}, status_code=400)
    with get_db() as conn:
        conn.execute(
            "UPDATE skills SET name=?, content=?, agent=? WHERE id=?",
            (name, content, agent, skill_id)
        )
        conn.commit()
    return {"ok": True}

@app.delete("/skills/{skill_id}")
async def delete_skill(skill_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        conn.commit()
    return {"ok": True}

# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
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

        chat_msgs = [{"role": m["role"], "content": m["content"]} for m in history if m["role"] in ("user", "assistant")]

        # Step 1: Manager (with injected skills)
        manager_system   = build_system_prompt("manager")
        manager_response = ""
        async for chunk in ollama_stream(manager_system, chat_msgs):
            manager_response += chunk
            yield sse({"type": "chunk", "agent": "manager", "chunk": chunk})

        # Step 2: Parse delegations
        delegations = re.findall(r'<delegate agent="(\w+)">(.*?)</delegate>', manager_response, re.DOTALL)

        # Step 3: Specialists (each with their own injected skills)
        specialist_results = {}
        for agent_key, task in delegations:
            if agent_key not in AGENTS:
                continue
            yield sse({"type": "status", "agent": agent_key, "status": "working"})
            system = build_system_prompt(agent_key)
            result = ""
            async for chunk in ollama_stream(system, [{"role": "user", "content": task.strip()}]):
                result += chunk
                yield sse({"type": "chunk", "agent": agent_key, "chunk": chunk})
            specialist_results[agent_key] = result
            yield sse({"type": "status", "agent": agent_key, "status": "done"})

        # Step 4: Synthesize
        final_response = manager_response
        if specialist_results:
            yield sse({"type": "status", "agent": "manager", "status": "synthesizing"})
            specialist_summary = "\n\n".join(f"[{AGENTS[k]['name']}]:\n{v}" for k, v in specialist_results.items())
            synthesis_prompt = f'The user asked: "{message}"\n\nSpecialist results:\n{specialist_summary}\n\nGive the user a single clear well-organized final answer.'
            synthesis_msgs = chat_msgs + [
                {"role": "assistant", "content": manager_response},
                {"role": "user",      "content": synthesis_prompt},
            ]
            synthesis = ""
            async for chunk in ollama_stream(manager_system, synthesis_msgs):
                synthesis += chunk
                yield sse({"type": "synthesis_chunk", "agent": "manager", "chunk": chunk})
            final_response = synthesis

        # Save
        history.append({"role": "assistant", "content": final_response, "agent": "manager"})
        title = message[:60] if sess["title"] == "New conversation" else sess["title"]
        save_session(session_id, title, history)

        yield sse({"type": "done", "sessionId": session_id, "delegations": [k for k, _ in delegations]})
        for k in AGENTS:
            yield sse({"type": "status", "agent": k, "status": "idle"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
@app.get("/github/repos")
async def gh_repos():
    try: return await github_request("GET", "/user/repos?sort=updated&per_page=30")
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/github/repos/{owner}/{repo}/contents")
async def gh_contents(owner: str, repo: str, path: str = ""):
    try: return await github_request("GET", f"/repos/{owner}/{repo}/contents/{path}")
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/github/repos/{owner}/{repo}/branches")
async def gh_branches(owner: str, repo: str):
    try: return await github_request("GET", f"/repos/{owner}/{repo}/branches")
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/github/repos/{owner}/{repo}/pulls")
async def gh_pulls(owner: str, repo: str):
    try: return await github_request("GET", f"/repos/{owner}/{repo}/pulls?state=open")
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/github/repos/{owner}/{repo}/commits")
async def gh_commit(owner: str, repo: str, request: Request):
    try:
        import base64
        body = await request.json()
        content_b64 = base64.b64encode(body["content"].encode()).decode()
        payload = {"message": body["message"], "content": content_b64, "branch": body.get("branch", "main")}
        if body.get("sha"): payload["sha"] = body["sha"]
        return await github_request("PUT", f"/repos/{owner}/{repo}/contents/{body['path']}", payload)
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/github/repos/{owner}/{repo}/pulls")
async def gh_create_pr(owner: str, repo: str, request: Request):
    try: return await github_request("POST", f"/repos/{owner}/{repo}/pulls", await request.json())
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/github/repos/{owner}/{repo}/pulls/{number}/reviews")
async def gh_review(owner: str, repo: str, number: int, request: Request):
    try: return await github_request("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", await request.json())
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

# ---------------------------------------------------------------------------
# Static (must be last)
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory="public", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print(f"""
🚀 Kal-AI running at http://localhost:{PORT}
   Model:  {OLLAMA_MODEL}
   Ollama: {OLLAMA_HOST}
   GitHub: {'connected' if GITHUB_TOKEN else 'no token'}

   Make sure Ollama is running: ollama serve
   And the model is pulled:     ollama pull {OLLAMA_MODEL}
""")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
