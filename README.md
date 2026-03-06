# Family Bot — Claude Agent SDK

AI family assistant on Telegram + WhatsApp, powered by the [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents/claude-agent-sdk).

Deploy one instance per family. All family-specific data lives in config files, not code.

## Features

- **Telegram + WhatsApp** — dual-channel messaging with real-time streaming UX
- **Gmail + Calendar** — email search, sending, calendar management via Google Workspace MCP
- **Browser automation** — screenshots, form filling, web research via Playwright MCP
- **Phone calls** — outbound AI voice calls via Vapi (multilingual, male/female voices)
- **Image generation & editing** — Imagen 4 + Gemini image editing via Google AI
- **RAG semantic search** — chunk-based conversation search with Gemini embeddings
- **Flexible scheduler** — conversationally managed scheduled tasks (morning brief, email monitor, etc.)
- **Memory system** — SQLite FTS5 knowledge base, facts, conversation history, media cache
- **Third-party correspondence** — bot can message contacts on behalf of the family (with approval)
- **Self-upgrade** — bot can edit its own code and redeploy from chat (admin only)
- **Architect principles** — 20-point quality checklist enforced on all code changes
- **Integration tests** — 41 tests covering health, git sync, self-upgrade, TG parsing, enrichment

## Prerequisites

### Server Requirements

- **OS**: Linux (Ubuntu 22.04+ recommended) or any Docker-capable host
- **RAM**: 4 GB minimum (16 GB recommended — the container memory limit is 16 GB)
- **Disk**: 10 GB free (Docker images + Chromium + data)
- **Docker**: Docker Engine 24+ with Compose v2
- **Public URL**: Required for Telegram webhooks — use a domain with HTTPS (e.g., via Cloudflare Tunnel, nginx + certbot, or a cloud load balancer). See `examples/nginx.conf` for a template.

### Required API Keys

| Service | Required? | How to get |
|---------|-----------|------------|
| **Claude** (Anthropic) | Yes | [Claude Pro/Team subscription](https://claude.ai) for OAuth token, or [API key](https://console.anthropic.com/) for pay-per-token |
| **Telegram Bot** | Yes | Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` |

### Optional API Keys

| Service | What it enables | How to get |
|---------|----------------|------------|
| **Google OAuth** | Gmail + Calendar | [Google Cloud Console](https://console.cloud.google.com/) → APIs → Credentials → OAuth 2.0 Client ID (Desktop app) |
| **Gemini** | Image generation + editing | [Google AI Studio](https://aistudio.google.com/apikey) |
| **Vapi** | AI phone calls | [vapi.ai](https://vapi.ai) dashboard + Twilio phone number |
| **WhatsApp** | WhatsApp messaging | No API key — uses built-in WhatsApp bridge (QR code pairing) |

## Quick Start

```bash
# 1. Fork this repo on GitHub (each family bot needs its own fork)
# 2. Clone YOUR fork to the server
git clone git@github.com:YOU/family-bot.git ~/my-family-bot
cd ~/my-family-bot

# 3. Run the interactive setup wizard
./scripts/setup.sh

# The wizard will:
#   - Create data/family_config.json (family members, goals)
#   - Create .env (API keys, chat IDs, Docker settings)
#   - Initialize data files (knowledge base, facts)
#   - Generate docker-compose.override.yml (deploy key for self-upgrade)
#   - Set up server-side git (deploy key, safe.directory)
#   - Install deploy watcher (systemd service)
#   - Build & start Docker containers
#   - Set Telegram webhook

# 4. Verify
curl http://localhost:8000/health
```

For partial setup: `./scripts/setup.sh --config` (config only) or `./scripts/setup.sh --deploy` (deploy only).

### Setting Up Google Workspace (Gmail + Calendar)

1. Create a Google Cloud project and enable the Gmail and Calendar APIs
2. Create an OAuth 2.0 Client ID (type: Desktop application)
3. Download the client secret JSON to `data/gmail-api/gmail_credentials.json`
4. Run the credential setup script (locally — it opens a browser):
   ```bash
   pip install google-auth-oauthlib google-api-python-client
   python scripts/setup_google_credentials.py
   ```
5. Copy the generated `data/google-workspace-creds/<email>.json` to the server
6. Set `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` in `.env`

### Claude Authentication

Two options:

**Option A — OAuth token (Claude Pro/Team subscription, recommended):**
```bash
# On your Mac (with Claude Code CLI installed):
./scripts/refresh_server_token.sh
```
This pushes credentials from your macOS Keychain to the server's Docker volume. The CLI auto-refreshes the token.

**Option B — API key (pay-per-token):**
```bash
# In .env:
ANTHROPIC_API_KEY=sk-ant-...
```

## Architecture

```
                        +-------------------------------------+
                        |  main.py (FastAPI)                  |
                        |  - TG webhook + streaming           |
                        |  - WA polling + periodic updates    |
                        |  - Scheduler (custom tasks)         |
                        +--------------+----------------------+
                                       |
                        +--------------v----------------------+
                        |  bot/agent.py - ClientPool          |
                        |  One ClaudeSDKClient per chat (24h) |
                        |  All messages -> Opus               |
                        |  Adaptive thinking (SDK decides)    |
                        +--------------+----------------------+
                                       |
           +----------+----------------+----------------+
           v          v                v                v
        Custom     External         External         External
        MCP (3)    MCP              MCP              MCP
        +--------+ +----------+    +-----------+    +----------+
        |Telegram| |Playwright|    | WhatsApp  |    |  Google  |
        |Phone   | |(npx)     |    |(Python)   |    |Workspace |
        |Memory  | |          |    |           |    |(pip)     |
        |RAG     | |          |    |           |    |          |
        |Schedule| |          |    |           |    |          |
        +--------+ +----------+    +-----------+    +----------+
```

**SDK built-in tools** (always available): `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `WebSearch`, `WebFetch`

## Multi-Family Setup

Each family bot runs from **its own fork** of this repository:

```
upstream/family-bot (this repo)
    ├── fork → github.com/alice/family-bot  → server: ~/alice-bot/
    ├── fork → github.com/bob/family-bot    → server: ~/bob-bot/
    └── fork → github.com/carol/family-bot  → server: ~/carol-bot/
```

**Why forks?** The bot's self-upgrade feature commits code changes and pushes to `origin`. Each family needs its own remote so their customizations don't collide.

Each fork has its own:
- `data/family_config.json` — identity, members, goals
- `.env` — API keys, chat IDs, Docker settings
- `docker-compose.override.yml` — deploy key mount for self-upgrade
- Docker volumes — conversations, knowledge, sessions
- `COMPOSE_PROJECT_NAME` + `BOT_PORT` — unique per family on same server

To add a new family:
1. Fork this repo
2. Clone the fork to the server
3. Run `./scripts/setup.sh` — wizard handles everything
4. Multiple bots coexist on one server via different ports

## Security

Defense-in-depth with four layers:
1. **Container isolation** — Docker, non-root `botuser`, no secrets in image
2. **User auth** — only configured user IDs/phones can trigger inference
3. **Admin tool gating** — Bash/Write/Edit require admin user context
4. **Bash blocklist** — blocks env dumps, secret file reads, network exfiltration

All credentials via `.env` (gitignored). No secrets in code or Docker image.

## Configuration

| File | Purpose | In git? |
|------|---------|---------|
| `data/family_config.json` | Family identity, members, goals | No (gitignored) |
| `.env` | API keys, chat IDs, user IDs | No (gitignored) |
| `docker-compose.override.yml` | Deploy key mount for self-upgrade | No (gitignored) |
| `data/prompts/*.txt` | System prompt fragments | Yes |
| `examples/family_config.json` | Template for new families | Yes |
| `examples/docker-compose.override.yml` | Template for deploy key setup | Yes |
| `examples/nginx.conf` | Nginx reverse proxy template | Yes |
| `.env.example` | Template for env vars | Yes |

## File Structure

```
family-bot/
├── main.py              # FastAPI app, webhooks, polling, scheduler
├── config.py            # Loads family_config.json + env vars
├── Dockerfile           # Python 3.12 + Node.js 22 + Playwright Chromium
├── docker-compose.yml   # bot-core + wa-bridge services
├── entrypoint.sh        # Privilege drop + git/SSH setup
├── .env.example         # Environment variables template
├── bot/
│   ├── agent.py         # ClientPool — one SDK client per chat
│   ├── mcp_tools.py     # 26 custom MCP tools (TG, WA, Phone, Memory, RAG, Scheduler)
│   ├── mcp_config.py    # External MCP server configs
│   ├── hooks.py         # Security hooks (admin gating, blocklists)
│   ├── prompts.py       # System prompt builder
│   ├── memory.py        # SQLite FTS5 memory system
│   ├── rag.py           # RAG v4: chunk-based semantic search (Gemini embeddings)
│   ├── scheduler.py     # Flexible scheduled tasks (JSON-backed, MCP-managed)
│   └── router.py        # Message effort routing
├── integrations/        # TG, WA, Vapi, Gemini clients
├── whatsapp-mcp-server/ # Python MCP server for WhatsApp
├── whatsapp-bridge/     # Go HTTP bridge (whatsmeow)
├── tests/               # Integration tests (41 tests)
├── examples/            # Template configs + nginx
├── data/prompts/        # 18 modular system prompt files
├── scripts/             # Setup wizard, credential management
└── deploy/              # Host-side deploy watcher (systemd)
```

## Testing

```bash
# Local tests only (TG parsing + message enrichment — no server needed):
python -m pytest tests/test_integration.py -v -k "Parsing or Enrichment"

# Full tests (requires server access):
BOT_SERVER=1.2.3.4 SSH_KEY=~/.ssh/mykey CONTAINER_NAME=mybot \
    python -m pytest tests/test_integration.py -v
```

## Deploy Watcher & Self-Upgrade

The bot can modify its own code from chat (admin only):
1. Bot edits files via Write/Edit tools (on `/host-repo` mount)
2. Bot commits and pushes to your fork
3. Bot calls `deploy_bot(action="rebuild")` → writes trigger file
4. Host-side watcher (systemd service) detects trigger → `git pull` → `docker build` → restart
5. Health check runs for 120s. On failure, auto-rollback to last known healthy commit

The deploy watcher is installed by `./scripts/setup.sh` (Step 6). See `deploy/host-watcher.sh` for details.

## Troubleshooting

### Bot not responding to messages
```bash
# Check container is running and healthy
docker compose ps
curl http://localhost:8000/health

# Check logs for errors
docker logs <container-name> --tail 50
```

### Container in restart loop
```bash
# Usually a Python syntax error (from self-upgrade). Check logs:
docker logs <container-name> --tail 30

# Fix: reset to last known good state
git log --oneline -5                   # Find good commit
git reset --hard <good-commit>
docker compose build --no-cache bot-core && docker compose up -d bot-core
```

### Telegram webhook not working
```bash
# Verify webhook is set
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"

# Re-set webhook
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://your-domain.com/webhook/telegram"}'
```

### WhatsApp bridge not connecting
```bash
# Check bridge logs for QR code (first-time pairing)
docker logs <container-name>-wa-bridge

# Scan QR code: WhatsApp → Settings → Linked Devices → Link a Device
# Session persists in Docker volume — pair once only
```

### Google Workspace (Gmail/Calendar) not working
- Ensure `data/google-workspace-creds/<email>.json` exists and has a valid refresh token
- Re-run `python scripts/setup_google_credentials.py` locally to refresh
- Copy the updated file to the server and restart

### Environment variables not taking effect
```bash
# docker compose restart does NOT re-read .env
# You must recreate the container:
docker compose down bot-core && docker compose up -d bot-core
```

## License

MIT — see [LICENSE](LICENSE).
