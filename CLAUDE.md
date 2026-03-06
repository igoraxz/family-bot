# Family Bot — Developer Guide

AI-powered family assistant running on Claude Agent SDK with Telegram and WhatsApp integration.

## Architecture Overview

```
family-bot/
  main.py           — FastAPI app: TG webhooks, WA polling, scheduler
  config.py         — Loads family_config.json + env vars, builds runtime config
  Dockerfile        — Python 3.12 + Node.js 22 + Docker CLI + Playwright Chromium
  docker-compose.yml — bot-core + wa-bridge services, volumes, networks
  entrypoint.sh     — Volume permission fixes, SSH/git setup, drops to botuser
  bot/
    agent.py        — ClientPool: one ClaudeSDKClient per chat, 24h keepalive
    hooks.py        — PreToolUse/PostToolUse hooks: admin gating, file/bash blocklists
    mcp_tools.py    — Custom MCP tools: TG, WA, phone, image gen, deploy
    mcp_config.py   — External MCP server configs: Playwright, WhatsApp, Google Workspace
    memory.py       — SQLite conversation/media storage, knowledge/facts/goals
    rag.py          — RAG: chunk-based semantic search (Gemini embeddings, cosine similarity)
    prompts.py      — System prompt builder (loads data/prompts/*.txt files)
    router.py       — Message effort routing
    scheduler.py    — Flexible scheduled tasks (JSON-backed, MCP-managed)
  integrations/     — Telegram, WhatsApp, Vapi (phone), Gemini, web clients
  whatsapp-mcp-server/ — Python MCP server for WhatsApp (runs as stdio subprocess)
  whatsapp-bridge/  — Go HTTP bridge (whatsmeow-based), manages WA session + messages.db
  data/prompts/     — System prompt fragments (mounted as volume, editable without rebuild)
```

## Multi-Family Design

All family-specific data lives outside the code:

| Data | Location | How it's loaded |
|------|----------|-----------------|
| Family identity, members, goals | `data/family_config.json` | `config.py` → `FAMILY_CONTEXT`, `PARENT_NAMES`, etc. |
| Secrets (API keys, tokens) | `.env` | Environment variables |
| User IDs, chat IDs | `.env` | `TG_ALLOWED_USERS`, `TG_CHAT_ID`, etc. |
| Prompts | `data/prompts/*.txt` | Loaded by `prompts.py`, placeholders filled from config |
| Knowledge / facts | `data/family_knowledge.md`, `data/family_facts.json` | Volume-mounted, bot reads/writes |
| Google credentials | `data/google-workspace-creds/` | Volume-mounted |

## Key Patterns

### Docker Volume Mounts
- `bot-data:/app/data` — persistent bot data (SQLite DB, state files)
- `wa-data:/app/wa-data:ro` — WA bridge data (messages.db), READ-ONLY in bot-core
- `wa-data:/app/store` — same volume, READ-WRITE in wa-bridge container
- `shared-media:/shared-media` — for sending files TO wa-bridge (bot writes, bridge reads)
- `claude-sessions:/home/botuser/.claude` — Agent SDK session persistence
- `.:/host-repo` — git repo mount for self-upgrade

**Path translation**: wa-bridge paths (`/app/store/...`) must be translated to `/app/wa-data/...` when used in bot-core.

### Agent SDK Usage
- One `ClaudeSDKClient` per chat session (NOT shared — no session isolation in SDK)
- `permission_mode="bypassPermissions"` (do NOT pass `allow_dangerously_skip_permissions=True` — it crashes the SDK)
- Hooks via `HookMatcher` — `PreToolUse` for security, `PostToolUse` for status
- `thinking={"type": "adaptive"}` always on
- `max_turns=75` per query (with "continue" mechanism for tasks exceeding the limit)
- Custom MCP tools via `create_sdk_mcp_server()` + `@tool` decorator

### Security Model (4 layers)
1. **Container isolation**: Docker, non-root `botuser`, no secrets in image
2. **User auth**: Admin user IDs checked in hooks
3. **Admin tool gating**: Bash/Write/Edit/deploy_bot/deploy_status require admin
4. **Bash blocklist**: Blocks env dumps, secret file reads, network exfiltration

### Message Flow
1. TG webhook or WA poll receives message
2. `process_incoming()` builds prompt, sets user context, calls `client_pool.query()`
3. TG: streaming placeholder edits. WA: periodic status updates
4. Fallback: if bot didn't call send tool, forward response text to chat
5. Post-processing: memory extraction, media caching

## Coding Rules

1. **Read before writing**: Always `Grep`/`Read` to understand existing code before editing
2. **Minimal changes**: Don't refactor surrounding code. Don't add comments or docstrings to code you didn't change
3. **No f-string newlines**: NEVER use literal newlines in f-strings — use `\n` escape sequences. SyntaxError crashes the bot with no recovery
4. **Test mentally**: Walk through your edit for syntax errors before writing. A SyntaxError = infinite restart loop
5. **Pin dependencies**: All versions are pinned in requirements.txt. NEVER update without explicit approval
6. **Admin-only operations**: deploy_bot, deploy_status, Bash, Write, Edit are admin-gated
7. **Python 3.12 typing**: Use `dict`, `list`, `str | None` — NOT `Dict`, `List`, `Optional`. Use `Callable` from typing (not `callable`)
8. **Async patterns**: Use `asyncio.create_task()` for fire-and-forget. Use `asyncio.Lock` per session key
9. **Error handling**: Report errors to chat (never fail silently). Use `log.error(..., exc_info=True)`
10. **File paths in container**: Edit in `/host-repo/`, NOT `/app/` (which is read-only image copy)
11. **Git discipline**: Each change = separate commit + push. Never accumulate uncommitted changes

## Common Pitfalls

- **WA media paths**: Bridge returns `/app/store/...`, bot-core sees `/app/wa-data/...` — always translate
- **Shared media for sending**: Copy to `/shared-media/`, send via bridge, then delete the copy
- **SDK `receive_response()`**: Takes NO arguments. Don't pass `include_partial` or similar
- **SDK `callable` bug**: Python 3.12 doesn't allow `callable` as type hint — use `typing.Callable`
- **Context vars**: `current_user_ctx`, `tool_status_callback`, `send_tool_called` are `contextvars.ContextVar` — must be set before each query
- **Entrypoint permissions**: New volumes/dirs need `chown botuser:botuser` in entrypoint.sh
- **Playwright**: Uses Chromium (bundled by Playwright, not system Chrome). Config uses `--browser chromium`

## Deploy Process

### Via deploy_bot tool (self-upgrade):
1. Edit code in `/host-repo/`
2. Verify syntax: `python3 -c "import ast; ast.parse(open('/host-repo/<file>').read())"`
3. Commit + push to fork
4. `deploy_bot(action="rebuild", reason="...")` triggers host watcher
5. Host watcher: git pull, docker build, restart, health check
6. On failure: auto-rollback to last healthy commit (`deploy/last_healthy_commit`)

### Manual:
```bash
git pull origin main
docker compose build --no-cache bot-core
docker compose up -d bot-core
```

## Testing Changes

Before deploying, verify:
1. No Python syntax errors: `python3 -c "import ast; ast.parse(open('<file>').read())"`
2. Import check: `python3 -c "from bot.<module> import <thing>"`
3. Health endpoint: `curl http://localhost:8000/health`
