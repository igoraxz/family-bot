# Family Bot — Architecture & Tuning Reference

For setup instructions see [README.md](README.md). For developer guide see [CLAUDE.md](CLAUDE.md).

## Module Responsibilities

| Module | Responsibility | Key Config |
|--------|---------------|------------|
| `main.py` | HTTP server, message routing, TG streaming, WA polling, scheduler loop | HOST, PORT, TG_CHAT_ID |
| `config.py` | Load family_config.json + env vars, expose all config constants | DATA_DIR, all *_API_KEY |
| `bot/agent.py` | SDK client lifecycle, session management, query execution | MAX_TURNS |
| `bot/hooks.py` | Security: admin gating, bash blocklist, tool status callbacks | ADMIN_USERS |
| `bot/mcp_tools.py` | Custom MCP tools (26 tools in 3 servers) | TOOL_TIMEOUT |
| `bot/mcp_config.py` | External MCP server configs (Playwright, WA, Google) | WA_BRIDGE_URL |
| `bot/memory.py` | SQLite storage: messages, facts, knowledge, summaries, media | DB_PATH, KNOWLEDGE_FILE |
| `bot/rag.py` | Semantic search: chunk embeddings, cosine similarity | RAG_* |
| `bot/scheduler.py` | Scheduled tasks: morning brief, email check, self-optimize | TASKS_FILE |
| `bot/prompts.py` | System prompt builder: loads data/prompts/*.txt, fills placeholders | PROMPTS_DIR |
| `bot/router.py` | Message effort routing (currently all Opus) | MODEL_QUICK, MODEL_LONG |
| `integrations/telegram.py` | TG Bot API client | TG_BOT_TOKEN |
| `integrations/whatsapp.py` | WA Bridge HTTP client | WA_API_URL |
| `integrations/phone.py` | Vapi voice agent client | VAPI_API_KEY |
| `integrations/gemini.py` | Gemini API for image gen/edit | GEMINI_API_KEY |
| `integrations/web.py` | Web search and fetch | — |

## Tunable Parameters

### RAG (config.py → bot/rag.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| RAG_CHUNK_SIZE | 7 | Messages per chunk window |
| RAG_CHUNK_STRIDE | 3 | Overlap between chunks |
| SIMILARITY_THRESHOLD | 0.25 | Minimum cosine similarity for results |
| RAG_EMBEDDING_MODEL | gemini-embedding-001 | Embedding model (3072-dim) |
| RAG_EMBEDDING_BATCH_SIZE | 100 | Texts per embedding batch |

### Agent (config.py → bot/agent.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| MAX_TURNS | 75 | Max tool-call iterations per query |
| SESSION_TIMEOUT | 86400 (24h) | SDK client keepalive duration |
| MAX_TOOL_RESULT_CHARS | 30000 | Truncation limit for tool outputs |
| TOOL_TIMEOUT | 90 | Per-tool execution timeout (seconds) |

### Streaming (main.py, config.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| STREAM_EDIT_INTERVAL | 3.0s | Min interval between TG message edits |
| LONG_STATUS_INTERVAL | 180s | WA status update frequency |
| LONG_SOFT_TIMEOUT | 600s | Soft timeout for long tasks |
| MAX_PARALLEL_TASKS | 3 | Concurrent tasks per chat |

### Memory (bot/memory.py, config.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| MEDIA_RETENTION_DAYS | 30 | Days to keep cached media |
| MAX_MEDIA_SIZE_MB | 20 | Max file size for media cache |

### Scheduler (bot/scheduler.py)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| Check interval | 30s | How often scheduler checks for due tasks |
| Default tasks | morning, email_check, evening, selfopt | Seeded if none exist |

Task times are configurable via the `manage_scheduled_task` MCP tool — no env vars needed.

## Data Flow

### Message Processing
1. TG webhook / WA poll receives message
2. Sender validated against TG_ALLOWED_USERS / WA_ALLOWED_PHONES
3. Message stored in SQLite via `memory.py`
4. `process_incoming()` builds system prompt, creates/reuses SDK client
5. SDK agent loop executes (tool calls, thinking, responses)
6. TG: streaming placeholder edits. WA: periodic status updates
7. Fallback: if agent didn't call send tool, forward response text to chat
8. Post-processing: memory extraction (knowledge/facts), media caching, RAG indexing

### Scheduled Tasks
1. `scheduler_loop()` runs every 30 seconds
2. `get_due_tasks()` checks time, day, last_run
3. For each due task: creates a fresh SDK client, executes task prompt
4. Task marks itself as run via `mark_task_run()`

## Data Files

```
data/
  prompts/              — System prompt fragments (00-17, loaded in order)
  family_config.json    — Family identity, members, goals (not in git)
  family_knowledge.md   — Natural language knowledge base (bot writes)
  family_facts.json     — Structured facts (bot writes)
  family_goals.json     — Goals tracking (bot writes)
  conversations.db      — SQLite: messages, embeddings, summaries, media
  scheduled_tasks.json  — Scheduled task definitions
  tmp/                  — Temporary files (screenshots, downloads)
  media_cache/          — Cached media files
  google-workspace-creds/ — Google OAuth credentials
```

## Known Issues

- WA media: bridge stores at `/app/store/`, bot-core reads from `/app/wa-data/` — path translation required
- WA images: reported as attached but files sometimes not found at mapped paths
