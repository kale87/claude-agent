# Claude Agent v3

A multi-agent Claude system with a pixel office UI and full GitHub integration.

## Setup

```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY and GITHUB_TOKEN
npm install
npm start
```

Open http://localhost:3000

## Agents

- **Manager** — receives your request, plans and delegates
- **Coder** — writes code, reviews, handles GitHub operations  
- **Researcher** — gathers context, summarizes, searches
- **Writer** — content, docs, commit messages

## GitHub Integration

Set `GITHUB_TOKEN` in `.env`. Then use the GitHub panel to browse repos, commit, push, create branches and PRs, and review pull requests.
