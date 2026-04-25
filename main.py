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
# GitHub keyword detector
# ---------------------------------------------------------------------------
def is_github_task(message: str) -> bool:
    msg = message.lower()
    keywords = ["repo","repos","repository","branch","branches","commit","push",
                "pull request","pr","merge","file","files","folder","read",
                "show me","list","what's in","what is in","contents","look at","check"]
    has_kw   = any(k in msg for k in keywords)
    has_repo = bool(re.search(r'\b(repo|repository|github|branch|commit|file|files|pr|merge)\b', msg))
    return has_kw and has_repo

# ---------------------------------------------------------------------------
# GitHub tools
# ---------------------------------------------------------------------------
GITHUB_TOOLS = [
    {"type":"function","function":{"name":"list_repos","description":"List all GitHub repositories. Use ONLY to list repos, NOT to see files inside a repo.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"list_branches","description":"List branches in a repo.","parameters":{"type":"object","properties":{"owner":{"type":"string"},"repo":{"type":"string"}},"required":["owner","repo"]}}},
    {"type":"function","function":{"name":"list_files","description":"List files and folders INSIDE a specific repo directory. Use for exploring repo contents, NOT for listing repos.","parameters":{"type":"object","properties":{"owner":{"type":"string"},"repo":{"type":"string"},"path":{"type":"string","description":"Directory path, empty for root"},"branch":{"type":"string"}},"required":["owner","repo"]}}},
    {"type":"function","function":{"name":"read_file","description":"Read a file's contents.","parameters":{"type":"object","properties":{"owner":{"type":"string"},"repo":{"type":"string"},"path":{"type":"string"},"branch":{"type":"string"}},"required":["owner","repo","path"]}}},
    {"type":"function","function":{"name":"commit_file","description":"Create or update a file (commits + pushes).","parameters":{"type":"object","properties":{"owner":{"type":"string"},"repo":{"type":"string"},"path":{"type":"string"},"content":{"type":"string"},"message":{"type":"string"},"branch":{"type":"string"}},"required":["owner","repo","path","content","message"]}}},
    {"type":"function","function":{"name":"create_branch","description":"Create a new branch.","parameters":{"type":"object","properties":{"owner":{"type":"string"},"repo":{"type":"string"},"branch":{"type":"string"},"from_branch":{"type":"string"}},"required":["owner","repo","branch"]}}},
    {"type":"function","function":{"name":"merge_branch","description":"Merge one branch into another.","parameters":{"type":"object","properties":{"owner":{"type":"string"},"repo":{"type":"string"},"base":{"type":"string"},"head":{"type":"string"},"message":{"type":"string"}},"required":["owner","repo","base","head"]}}},
    {"type":"function","function":{"name":"create_pr","description":"Open a pull request.","parameters":{"type":"object","properties":{"owner":{"type":"string"},"repo":{"type":"string"},"title":{"type":"string"},"head":{"type":"string"},"base":{"type":"string"},"body":{"type":"string"}},"required":["owner","repo","title","head"]}}},
    {"type":"function","function":{"name":"merge_pr","description":"Merge a pull request.","parameters":{"type":"object","properties":{"owner":{"type":"string"},"repo":{"type":"string"},"number":{"type":"integer"},"method":{"type":"string"}},"required":["owner","repo","number"]}}},
    {"type":"function","function":{"name":"list_prs","description":"List pull requests.","parameters":{"type":"object","properties":{"owner":{"type":"string"},"repo":{"type":"string"},"state":{"type":"string"}},"required":["owner","repo"]}}},
]

# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------
async def gh(method: str, endpoint: str, body: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "User-Agent": "kal-ai/1.0",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(method, f"https://api.github.com{endpoint}", headers=headers, json=body)
        if resp.status_code == 204: return {}
        data = resp.json()
        if resp.status_code >= 400: raise Exception(data.get("message", f"GitHub error {resp.status_code}"))
        return data

async def run_tool(name: str, params: dict) -> str:
    try:
        if name == "list_repos":
            data = await gh("GET", "/user/repos?sort=updated&per_page=50")
            return json.dumps([{"name":r["name"],"full_name":r["full_name"],"default_branch":r["default_branch"],"description":r.get("description",""),"private":r["private"]} for r in data], indent=2)
        elif name == "list_branches":
            data = await gh("GET", f"/repos/{params['owner']}/{params['repo']}/branches")
            return json.dumps([b["name"] for b in data])
        elif name == "list_files":
            path = params.get("path","").lstrip("/")
            branch = params.get("branch","")
            ep = f"/repos/{params['owner']}/{params['repo']}/contents/{path}"
            if branch: ep += f"?ref={branch}"
            data = await gh("GET", ep)
            if isinstance(data, list):
                return json.dumps([{"name":f["name"],"type":f["type"],"path":f["path"]} for f in data], indent=2)
            return json.dumps(data)
        elif name == "read_file":
            branch = params.get("branch","")
            ep = f"/repos/{params['owner']}/{params['repo']}/contents/{params['path']}"
            if branch: ep += f"?ref={branch}"
            data = await gh("GET", ep)
            content = base64.b64decode(data["content"].replace("\n","")).decode("utf-8",errors="replace")
            return json.dumps({"path":params["path"],"sha":data["sha"],"content":content})
        elif name == "commit_file":
            branch = params.get("branch","main")
            body: dict = {"message":params["message"],"content":base64.b64encode(params["content"].encode()).decode(),"branch":branch}
            try:
                existing = await gh("GET", f"/repos/{params['owner']}/{params['repo']}/contents/{params['path']}?ref={branch}")
                body["sha"] = existing["sha"]
            except: pass
            result = await gh("PUT", f"/repos/{params['owner']}/{params['repo']}/contents/{params['path']}", body)
            return json.dumps({"ok":True,"commit":result.get("commit",{}).get("sha",""),"path":params["path"],"branch":branch})
        elif name == "create_branch":
            from_branch = params.get("from_branch","main")
            ref_data = await gh("GET", f"/repos/{params['owner']}/{params['repo']}/git/ref/heads/{from_branch}")
            sha = ref_data["object"]["sha"]
            await gh("POST", f"/repos/{params['owner']}/{params['repo']}/git/refs", {"ref":f"refs/heads/{params['branch']}","sha":sha})
            return json.dumps({"ok":True,"branch":params["branch"],"from":from_branch})
        elif name == "merge_branch":
            result = await gh("POST", f"/repos/{params['owner']}/{params['repo']}/merges", {"base":params["base"],"head":params["head"],"commit_message":params.get("message",f"Merge {params['head']} into {params['base']}")})
            return json.dumps({"ok":True,"sha":result.get("sha","")})
        elif name == "create_pr":
            result = await gh("POST", f"/repos/{params['owner']}/{params['repo']}/pulls", {"title":params["title"],"head":params["head"],"base":params.get("base","main"),"body":params.get("body","")})
            return json.dumps({"ok":True,"number":result["number"],"url":result["html_url"]})
        elif name == "merge_pr":
            result = await gh("PUT", f"/repos/{params['owner']}/{params['repo']}/pulls/{params['number']}/merge", {"merge_method":params.get("method","squash")})
            return json.dumps({"ok":True,"merged":result.get("merged",False),"sha":result.get("sha","")})
        elif name == "list_prs":
            state = params.get("state","open")
            data = await gh("GET", f"/repos/{params['owner']}/{params['repo']}/pulls?state={state}&per_page=20")
            return json.dumps([{"number":p["number"],"title":p["title"],"head":p["head"]["ref"],"base":p["base"]["ref"],"state":p["state"]} for p in data], indent=2)
        else:
            return json.dumps({"error":f"Unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error":str(e)})

# ---------------------------------------------------------------------------
# Format tool results into readable markdown
# ---------------------------------------------------------------------------
def format_tool_result(name: str, args: dict, result: str) -> str:
    try: data = json.loads(result)
    except: data = result

    if name == "list_repos" and isinstance(data, list):
        lines = ["**Your repositories:**"]
        for r in data:
            priv = " \U0001f512" if r.get("private") else ""
            desc = f" \u2014 {r['description']}" if r.get("description") else ""
            lines.append(f"- **{r['name']}**{priv}{desc}")
        return "\n".join(lines)

    elif name == "list_files" and isinstance(data, list):
        repo = args.get("repo","")
        path = args.get("path","") or "root"
        lines = [f"**Files in `{repo}/{path}`:**"]
        for f in data:
            icon = "\U0001f4c1" if f.get("type")=="dir" else "\U0001f4c4"
            lines.append(f"- {icon} `{f['name']}`")
        return "\n".join(lines)

    elif name == "list_branches" and isinstance(data, list):
        repo = args.get("repo","")
        lines = [f"**Branches in `{repo}`:**"]
        for b in data: lines.append(f"- `{b}`")
        return "\n".join(lines)

    elif name == "read_file" and isinstance(data, dict) and "content" in data:
        path = data.get("path","")
        content = data["content"]
        ext = path.split(".")[-1] if "." in path else ""
        truncated = content[:3000] + ("\n..." if len(content)>3000 else "")
        return f"**`{path}`:**\n```{ext}\n{truncated}\n```"

    elif name == "commit_file" and isinstance(data, dict) and data.get("ok"):
        return f"\u2705 Committed `{data.get('path')}` to `{data.get('branch')}` (commit `{data.get('commit','')[:7]}`)"

    elif name == "create_branch" and isinstance(data, dict) and data.get("ok"):
        return f"\u2705 Created branch `{data.get('branch')}` from `{data.get('from')}`"

    elif name == "create_pr" and isinstance(data, dict) and data.get("ok"):
        return f"\u2705 PR #{data.get('number')} created: {data.get('url')}"

    elif name == "merge_pr" and isinstance(data, dict) and data.get("ok"):
        return f"\u2705 PR merged (commit `{data.get('sha','')[:7]}`)"

    elif name == "list_prs" and isinstance(data, list):
        repo = args.get("repo","")
        lines = [f"**Open PRs in `{repo}`:**"]
        if not data: lines.append("No open PRs.")
        for p in data: lines.append(f"- #{p['number']} **{p['title']}** (`{p['head']}` \u2192 `{p['base']}`)") 
        return "\n".join(lines)

    elif isinstance(data, dict) and data.get("error"):
        return f"\u274c Error: {data['error']}"

    return f"```\n{result[:500]}\n```"

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
AGENTS = {
    "manager": {"name":"Manager","emoji":"🎯","color":"#6366f1","tools":False,"system":"""You are the Manager agent of Kal-AI.\n\nRules:\n1. For SIMPLE QUESTIONS about general knowledge \u2014 answer directly.\n2. For ANY GitHub task \u2014 delegate to coder.\n3. For research \u2014 delegate to researcher.\n4. For writing \u2014 delegate to writer.\n5. NEVER invent information.\n\nTo delegate:\n<delegate agent=\"coder\">task</delegate>\n\nAfter specialists finish, present ONLY what they found."""},
    "coder": {"name":"Coder","emoji":"💻","color":"#10b981","tools":True,"system":"""You are the Coder agent. GitHub username: kale87.\n\nALWAYS use tools \u2014 never guess.\n- list_repos \u2192 list all repos\n- list_files \u2192 files inside a repo\n- read_file \u2192 read a file\n- commit_file \u2192 save changes\n\nNEVER use list_repos to see files inside a repo."""},
    "researcher": {"name":"Researcher","emoji":"🔍","color":"#f59e0b","tools":True,"system":"You are the Researcher agent. GitHub username: kale87. Use tools, report exactly what they return."},
    "writer": {"name":"Writer","emoji":"✍️","color":"#ec4899","tools":False,"system":"You are the Writer agent. Write content based only on information given to you."},
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
    return "\n\n---\n## Your Skills\n" + "\n\n".join(f"### Skill: {r['name']}\n{r['content']}" for r in rows)

def build_system(agent_key):
    return AGENTS[agent_key]["system"] + get_skills(agent_key)

# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
async def ollama_call(system, messages, tools=None):
    payload = {"model":OLLAMA_MODEL,"messages":[{"role":"system","content":system}]+messages,"stream":False}
    if tools: payload["tools"] = tools
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{OLLAMA_HOST}/api/chat", json=payload)
        return resp.json().get("message", {"role":"assistant","content":""})

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
# Agent runner — yields SSE events directly (real-time)
# ---------------------------------------------------------------------------
async def agent_sse(agent_key: str, task: str, sse_fn):
    """
    Run agent with tools, yielding SSE strings in real time.
    Returns final text.
    """
    system   = build_system(agent_key)
    tools    = GITHUB_TOOLS if AGENTS[agent_key]["tools"] else None
    messages = [{"role":"user","content":task}]
    full_text = ""
    last_tool_results = []

    for _ in range(10):
        msg = await ollama_call(system, messages, tools)
        tool_calls = msg.get("tool_calls", [])

        if not tool_calls:
            content = msg.get("content","").strip()
            # If model returned nothing after tools, format results ourselves
            if not content and last_tool_results:
                for name, args, result in last_tool_results:
                    content += format_tool_result(name, args, result) + "\n\n"
                content = content.strip()
            full_text += content
            yield sse_fn({"type":"chunk","agent":agent_key,"chunk":content})
            break

        messages.append({"role":"assistant","content":msg.get("content",""),"tool_calls":tool_calls})
        last_tool_results = []

        for tc in tool_calls:
            fn   = tc.get("function",{})
            name = fn.get("name","")
            args = fn.get("arguments",{})
            if isinstance(args, str):
                try: args = json.loads(args)
                except: args = {}

            # Emit tool_call immediately
            yield sse_fn({"type":"tool_call","agent":agent_key,"tool":name,"params":args})

            result = await run_tool(name, args)
            last_tool_results.append((name, args, result))

            # Emit tool_result immediately
            try: parsed = json.loads(result)
            except: parsed = result
            yield sse_fn({"type":"tool_result","agent":agent_key,"tool":name,"result":parsed})

            messages.append({"role":"tool","content":result})

    return full_text

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

# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
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

        # FAST PATH: GitHub task — go straight to Coder
        if is_github_task(message):
            yield sse({"type":"status","agent":"coder","status":"working"})
            final_text = ""
            async for event in agent_sse("coder", message, sse):
                yield event
                # capture final text from chunk events
                try:
                    d = json.loads(event[6:])  # strip "data: "
                    if d.get("type") == "chunk":
                        final_text += d.get("chunk","")
                except: pass
            history.append({"role":"assistant","content":final_text,"agent":"coder"})
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
            final_text = ""
            async for event in agent_sse(agent_key, task.strip(), sse):
                yield event
                try:
                    d = json.loads(event[6:])
                    if d.get("type") == "chunk":
                        final_text += d.get("chunk","")
                except: pass
            specialist_results[agent_key] = final_text
            yield sse({"type":"status","agent":agent_key,"status":"done"})

        yield sse({"type":"status","agent":"manager","status":"synthesizing"})
        summary = "\n\n".join(f"[{AGENTS[k]['name']} output]:\n{v}" for k,v in specialist_results.items())
        synth_msgs = chat_msgs + [{"role":"assistant","content":manager_response},{"role":"user","content":f'User asked: "{message}"\n\nSpecialist results:\n{summary}\n\nPresent clearly, do not invent anything.'}]
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

# ---------------------------------------------------------------------------
# GitHub panel API
# ---------------------------------------------------------------------------
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

   Make sure Ollama is running: ollama serve
""")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
