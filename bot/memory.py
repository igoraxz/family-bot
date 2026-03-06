"""Memory system — knowledge base, facts, conversation DB, media cache, and context engine.

Provides searchable persistent storage for ALL family data:
- Messages: every user/assistant message (FTS5 indexed)
- Facts: structured key-value facts (FTS5 indexed in SQLite + JSON file)
- Knowledge: natural language insights (FTS5 indexed in SQLite + markdown file)
- Summaries: auto-generated conversation summaries (FTS5 indexed)
- Media: cached media files with metadata (FTS5 indexed)
"""

import asyncio
import json
import logging
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from config import (
    KNOWLEDGE_FILE, FACTS_FILE, GOALS_FILE, DB_PATH, DEFAULT_GOALS,
    MEDIA_CACHE_DIR, MEDIA_RETENTION_DAYS, FAMILY_NAME, FAMILY_TIMEZONE,
    FAMILY_CONTEXT,
)

log = logging.getLogger(__name__)
TZ = ZoneInfo(FAMILY_TIMEZONE)


# === KNOWLEDGE BASE (natural language long-term memory) ===

def load_knowledge() -> str:
    try:
        return KNOWLEDGE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        initial = _build_initial_knowledge()
        KNOWLEDGE_FILE.write_text(initial, encoding="utf-8")
        return initial


def _build_initial_knowledge() -> str:
    """Build initial knowledge base from family_config.json."""
    lines = [f"# {FAMILY_NAME} Family Knowledge Base\n"]
    lines.append("This file contains everything learned about the family from conversations.\n")

    # Members section
    family = FAMILY_CONTEXT.get("family", {})
    member_lines = []
    for role in ("father", "mother"):
        member = family.get(role, {})
        if member.get("name"):
            member_lines.append(f"- {member['name']} ({role})")
    for child in family.get("children", []):
        desc = child.get("name", "")
        if child.get("school"):
            desc += f" — {child['school']}"
        if child.get("class"):
            desc += f", class {child['class']}"
        if child.get("note"):
            desc += f" ({child['note']})"
        member_lines.append(f"- {desc}")

    if member_lines:
        lines.append("## Family Members")
        lines.extend(member_lines)
        lines.append("")

    # Location
    location = FAMILY_CONTEXT.get("location", "")
    if location:
        lines.append("## Location")
        lines.append(f"- Home: {location}")
        lines.append("")

    return "\n".join(lines)


def save_knowledge_update(update_text: str):
    """Validate and save a knowledge base update (to file AND SQLite)."""
    text = update_text.strip()

    # Validation: reject empty, too short, too long, or suspicious updates
    if len(text) < 10:
        log.warning(f"Knowledge update rejected: too short ({len(text)} chars)")
        return
    if len(text) > 5000:
        log.warning(f"Knowledge update truncated from {len(text)} to 5000 chars")
        text = text[:5000]
    # Reject if it looks like raw JSON dump or contains sensitive data
    if text.startswith("{") or text.startswith("["):
        log.warning("Knowledge update rejected: looks like raw JSON (use FACTS_UPDATE instead)")
        return
    sensitive_patterns = ["API_KEY", "SECRET", "PASSWORD", "TOKEN", "sk-ant-"]
    if any(p in text.upper() for p in sensitive_patterns[:4]) or "sk-ant-" in text:
        log.warning("Knowledge update rejected: contains potentially sensitive data")
        return

    existing = load_knowledge()
    timestamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    updated = existing.rstrip() + f"\n\n---\n_Updated {timestamp}_\n{text}\n"
    KNOWLEDGE_FILE.write_text(updated, encoding="utf-8")
    log.info(f"Knowledge updated ({len(text)} chars)")


# === STRUCTURED FACTS (JSON + SQLite) ===

def load_facts() -> dict:
    try:
        return json.loads(FACTS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_facts_update(new_facts: dict):
    """Validate and save structured facts update (to file AND SQLite)."""
    if not isinstance(new_facts, dict):
        log.warning(f"Facts update rejected: expected dict, got {type(new_facts).__name__}")
        return
    if not new_facts:
        log.warning("Facts update rejected: empty dict")
        return
    # Reject oversized updates (max 50 keys per update)
    if len(new_facts) > 50:
        log.warning(f"Facts update rejected: too many keys ({len(new_facts)})")
        return
    # Validate each key-value pair
    sanitized = {}
    for key, value in new_facts.items():
        # Skip internal keys
        if key.startswith("_"):
            continue
        # Keys must be reasonable strings
        if not isinstance(key, str) or len(key) > 100:
            log.warning(f"Facts key rejected: '{str(key)[:50]}' (invalid or too long)")
            continue
        # Values should be serializable and not too large
        val_str = str(value)
        if len(val_str) > 2000:
            log.warning(f"Facts value for '{key}' truncated from {len(val_str)} chars")
            value = val_str[:2000]
        # Reject sensitive-looking values
        if isinstance(value, str) and ("sk-ant-" in value or "Bearer " in value):
            log.warning(f"Facts value for '{key}' rejected: looks like a credential")
            continue
        sanitized[key] = value

    if not sanitized:
        log.warning("Facts update rejected: no valid keys after validation")
        return

    existing = load_facts()
    existing.update(sanitized)
    existing["_last_updated"] = datetime.now(TZ).isoformat()
    FACTS_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    log.info(f"Facts updated: {list(sanitized.keys())}")


# === GOALS ===

def load_goals() -> list:
    try:
        data = json.loads(GOALS_FILE.read_text())
        return data if isinstance(data, list) else DEFAULT_GOALS
    except (FileNotFoundError, json.JSONDecodeError):
        GOALS_FILE.write_text(json.dumps(DEFAULT_GOALS, indent=2, ensure_ascii=False))
        return DEFAULT_GOALS


# === EXTRACT MEMORY UPDATES FROM CLAUDE OUTPUT ===

def extract_memory_updates(text: str):
    """Parse KNOWLEDGE_UPDATE and FACTS_UPDATE from Claude's response text."""
    # Knowledge updates (triple-quoted)
    for match in re.findall(r'KNOWLEDGE_UPDATE:\s*"""(.*?)"""', text, re.DOTALL):
        if match.strip():
            save_knowledge_update(match.strip())

    # Facts updates (JSON dict)
    for match in re.findall(r'FACTS_UPDATE:\s*(\{.*?\})', text, re.DOTALL):
        try:
            data = json.loads(match)
            if data:
                save_facts_update(data)
        except json.JSONDecodeError:
            log.warning(f"Failed to parse facts: {match[:100]}")


# === CONVERSATION DATABASE (SQLite + FTS5) ===

async def init_db():
    """Initialize the conversation database with FTS5 support for all memory types."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executescript("""
            -- Messages table (core conversation log)
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,           -- 'telegram' or 'whatsapp'
                user_name TEXT NOT NULL,
                user_id TEXT,
                text TEXT NOT NULL,
                role TEXT NOT NULL,              -- 'user' or 'assistant'
                timestamp TEXT NOT NULL,
                message_id TEXT,                 -- platform-specific message ID
                session_id TEXT                  -- conversation session ID
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                message_count INTEGER DEFAULT 0,
                summary TEXT
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                text,
                user_name,
                content=messages,
                content_rowid=id
            );

            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, text, user_name)
                VALUES (new.id, new.text, new.user_name);
            END;

            -- Summaries table (auto-generated conversation summaries)
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
                summary,
                content=summaries,
                content_rowid=id
            );

            CREATE TRIGGER IF NOT EXISTS summaries_ai AFTER INSERT ON summaries BEGIN
                INSERT INTO summaries_fts(rowid, summary)
                VALUES (new.id, new.summary);
            END;

            -- Facts log table (searchable structured facts history)
            CREATE TABLE IF NOT EXISTS facts_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_key TEXT NOT NULL,
                fact_value TEXT NOT NULL,
                category TEXT DEFAULT '',        -- e.g. 'school', 'travel', 'health'
                created_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS facts_log_fts USING fts5(
                fact_key,
                fact_value,
                category,
                content=facts_log,
                content_rowid=id
            );

            CREATE TRIGGER IF NOT EXISTS facts_log_ai AFTER INSERT ON facts_log BEGIN
                INSERT INTO facts_log_fts(rowid, fact_key, fact_value, category)
                VALUES (new.id, new.fact_key, new.fact_value, new.category);
            END;

            -- Knowledge log table (searchable knowledge snippets)
            CREATE TABLE IF NOT EXISTS knowledge_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_log_fts USING fts5(
                content,
                content=knowledge_log,
                content_rowid=id
            );

            CREATE TRIGGER IF NOT EXISTS knowledge_log_ai AFTER INSERT ON knowledge_log BEGIN
                INSERT INTO knowledge_log_fts(rowid, content)
                VALUES (new.id, new.content);
            END;

            -- Media cache index (searchable media file metadata)
            CREATE TABLE IF NOT EXISTS media_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,          -- stored filename in cache dir
                original_filename TEXT,          -- original name from sender
                media_type TEXT NOT NULL,        -- photo, document, voice, video, etc.
                source TEXT NOT NULL,            -- telegram or whatsapp
                chat_id TEXT,                    -- chat/group where media was sent
                sender_name TEXT,                -- who sent it
                description TEXT DEFAULT '',     -- auto-generated or caption text
                file_size INTEGER DEFAULT 0,
                mime_type TEXT DEFAULT '',
                cached_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,        -- retention-based expiry
                file_path TEXT NOT NULL          -- full path in media_cache dir
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS media_cache_fts USING fts5(
                filename,
                original_filename,
                description,
                sender_name,
                media_type,
                content=media_cache,
                content_rowid=id
            );

            CREATE TRIGGER IF NOT EXISTS media_cache_ai AFTER INSERT ON media_cache BEGIN
                INSERT INTO media_cache_fts(rowid, filename, original_filename, description, sender_name, media_type)
                VALUES (new.id, new.filename, new.original_filename, new.description, new.sender_name, new.media_type);
            END;

            -- Active sessions (persisted for restart recovery)
            CREATE TABLE IF NOT EXISTS active_sessions (
                session_key TEXT PRIMARY KEY,
                messages TEXT NOT NULL,
                last_activity REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        await db.commit()
    log.info(f"Conversation DB initialized (with facts_log, knowledge_log, media_cache): {DB_PATH}")

    # Index existing facts from JSON file into facts_log (one-time sync)
    await _sync_facts_to_db()
    # Index existing knowledge from markdown file into knowledge_log (one-time sync)
    await _sync_knowledge_to_db()

    # Add msg_type column if missing (v4 RAG needs it)
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            cursor = await db.execute("PRAGMA table_info(messages)")
            columns = {row[1] for row in await cursor.fetchall()}
            if "msg_type" not in columns:
                await db.execute("ALTER TABLE messages ADD COLUMN msg_type TEXT DEFAULT 'user_message'")
                await db.commit()
                log.info("Added msg_type column to messages table")
    except Exception as e:
        log.warning(f"msg_type column migration skipped: {e}")

    # Initialize RAG chunks table
    try:
        from bot.rag import init_rag_tables
        await init_rag_tables()
    except Exception as e:
        log.warning(f"RAG init skipped: {e}")


async def _sync_facts_to_db():
    """Sync facts from JSON file to facts_log table (idempotent — only adds missing)."""
    facts = load_facts()
    if not facts:
        return
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM facts_log")
        count = (await cursor.fetchone())[0]
        if count > 0:
            return  # Already synced — skip (facts_log is append-only)

        now = datetime.now(TZ).isoformat()
        for key, value in facts.items():
            if key.startswith("_"):
                continue
            category = _guess_fact_category(key)
            await db.execute(
                "INSERT INTO facts_log (fact_key, fact_value, category, created_at) VALUES (?, ?, ?, ?)",
                (key, str(value), category, now),
            )
        await db.commit()
        log.info(f"Synced {len(facts)} facts from JSON to facts_log table")


async def _sync_knowledge_to_db():
    """Sync knowledge entries to knowledge_log table (idempotent — only if empty)."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM knowledge_log")
        count = (await cursor.fetchone())[0]
        if count > 0:
            return  # Already synced

        knowledge = load_knowledge()
        if not knowledge or len(knowledge) < 50:
            return

        # Split by sections separated by "---"
        sections = re.split(r'\n---\n', knowledge)
        now = datetime.now(TZ).isoformat()
        for section in sections:
            text = section.strip()
            if len(text) > 20:
                await db.execute(
                    "INSERT INTO knowledge_log (content, created_at) VALUES (?, ?)",
                    (text[:5000], now),
                )
        await db.commit()
        log.info(f"Synced {len(sections)} knowledge sections to knowledge_log table")


def _guess_fact_category(key: str) -> str:
    """Heuristically guess a category from a fact key name."""
    key_lower = key.lower()
    if any(w in key_lower for w in ("school", "tutor", "club", "camp", "class", "homework")):
        return "school"
    if any(w in key_lower for w in ("trip", "flight", "hotel", "travel", "visa", "car_rental")):
        return "travel"
    if any(w in key_lower for w in ("passport", "licence", "id_")):
        return "documents"
    if any(w in key_lower for w in ("car_", "repair", "vehicle")):
        return "vehicle"
    if any(w in key_lower for w in ("bot_", "whatsapp", "telegram", "gmail")):
        return "bot"
    if any(w in key_lower for w in ("family", "birthday", "parent", "child")):
        return "family"
    if any(w in key_lower for w in ("excursion", "restaurant", "dinner", "activity")):
        return "activities"
    return "general"


async def store_message(source: str, user_name: str, text: str, role: str,
                        user_id: str = "", message_id: str = "", session_id: str = "",
                        msg_type: str = ""):
    """Store a message in the conversation database.

    msg_type values: user_message, bot_reply, status, placeholder, system.
    If not provided, defaults based on role (user->user_message, assistant->bot_reply).
    """
    if not msg_type:
        msg_type = "bot_reply" if role == "assistant" else "user_message"
    async with aiosqlite.connect(str(DB_PATH)) as db:
        # Check if msg_type column exists (migration may not have run yet)
        cursor = await db.execute("PRAGMA table_info(messages)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "msg_type" in columns:
            cursor = await db.execute(
                "INSERT INTO messages (source, user_name, user_id, text, role, timestamp, message_id, session_id, msg_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, user_name, user_id, text[:5000], role,
                 datetime.now(TZ).isoformat(), message_id, session_id, msg_type)
            )
        else:
            cursor = await db.execute(
                "INSERT INTO messages (source, user_name, user_id, text, role, timestamp, message_id, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (source, user_name, user_id, text[:5000], role,
                 datetime.now(TZ).isoformat(), message_id, session_id)
            )
        db_id = cursor.lastrowid
        await db.commit()
    return db_id


async def store_fact_in_db(key: str, value: str, category: str = ""):
    """Store a fact entry in the searchable facts_log table."""
    if not category:
        category = _guess_fact_category(key)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO facts_log (fact_key, fact_value, category, created_at) VALUES (?, ?, ?, ?)",
            (key, str(value)[:2000], category, datetime.now(TZ).isoformat()),
        )
        await db.commit()


async def store_knowledge_in_db(content: str):
    """Store a knowledge entry in the searchable knowledge_log table."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO knowledge_log (content, created_at) VALUES (?, ?)",
            (content[:5000], datetime.now(TZ).isoformat()),
        )
        await db.commit()


async def get_recent_messages(limit: int = 20) -> list[dict]:
    """Get the most recent messages for conversation context."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT source, user_name, text, role, timestamp FROM messages "
            "ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
    # Reverse to chronological order
    return [dict(r) for r in reversed(rows)]


def _sanitize_fts_query(query: str) -> str:
    """Escape FTS5 special characters to prevent query syntax errors.

    Wraps each word in double quotes for literal matching.
    """
    # Remove FTS5 operators and special chars, keep alphanumeric + spaces
    words = re.findall(r'[\w]+', query)
    if not words:
        return '""'
    # Quote each word for exact matching (avoids AND/OR/NOT interpretation)
    return " ".join(f'"{w}"' for w in words[:10])  # limit to 10 words


async def search_messages(query: str, limit: int = 10) -> list[dict]:
    """Search past messages using FTS5 (BM25 ranking)."""
    safe_query = _sanitize_fts_query(query)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT m.id, m.source, m.user_name, m.text, m.role, m.timestamp, "
            "rank FROM messages_fts f "
            "JOIN messages m ON f.rowid = m.id "
            "WHERE messages_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (safe_query, limit)
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_messages_around(message_id: int, before: int = 5, after: int = 5) -> list[dict]:
    """Get messages surrounding a specific message ID for full context."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, source, user_name, text, role, timestamp FROM messages "
            "WHERE id BETWEEN ? AND ? ORDER BY id",
            (message_id - before, message_id + after)
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def search_summaries(query: str, limit: int = 5) -> list[dict]:
    """Search past session summaries using FTS5."""
    safe_query = _sanitize_fts_query(query)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT s.summary, s.created_at, rank FROM summaries_fts f "
            "JOIN summaries s ON f.rowid = s.id "
            "WHERE summaries_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (safe_query, limit)
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def search_facts(query: str, limit: int = 10) -> list[dict]:
    """Search facts_log using FTS5 (BM25 ranking)."""
    safe_query = _sanitize_fts_query(query)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT fl.fact_key, fl.fact_value, fl.category, fl.created_at, rank "
            "FROM facts_log_fts f "
            "JOIN facts_log fl ON f.rowid = fl.id "
            "WHERE facts_log_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (safe_query, limit)
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def search_knowledge(query: str, limit: int = 5) -> list[dict]:
    """Search knowledge_log using FTS5 (BM25 ranking)."""
    safe_query = _sanitize_fts_query(query)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT kl.content, kl.created_at, rank "
            "FROM knowledge_log_fts f "
            "JOIN knowledge_log kl ON f.rowid = kl.id "
            "WHERE knowledge_log_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (safe_query, limit)
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def search_all_memory(query: str, limit: int = 15) -> dict:
    """Unified search across ALL memory types — the main entry point for memory retrieval.

    Searches messages, facts, knowledge, summaries, and media in parallel.
    Returns a combined dict with categorized results.
    """
    import asyncio

    results = await asyncio.gather(
        search_messages(query, limit=limit),
        search_facts(query, limit=min(limit, 10)),
        search_knowledge(query, limit=min(limit, 5)),
        search_summaries(query, limit=min(limit, 5)),
        search_media_cache(query, limit=min(limit, 5)),
        return_exceptions=True,
    )

    output = {
        "messages": results[0] if not isinstance(results[0], Exception) else [],
        "facts": results[1] if not isinstance(results[1], Exception) else [],
        "knowledge": results[2] if not isinstance(results[2], Exception) else [],
        "summaries": results[3] if not isinstance(results[3], Exception) else [],
        "media": results[4] if not isinstance(results[4], Exception) else [],
    }

    total = sum(len(v) for v in output.values())
    log.info(f"search_all_memory('{query[:50]}') found {total} results across all types")
    return output


async def store_summary(session_id: str, summary: str):
    """Store a session summary."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO summaries (session_id, summary, created_at) VALUES (?, ?, ?)",
            (session_id, summary, datetime.now(TZ).isoformat())
        )
        await db.commit()
    log.info(f"Summary stored for session {session_id[:8]}")


async def save_session_to_db(session_key: str, messages_json: str, last_activity: float):
    """Save or update an active session to disk for restart recovery."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT OR REPLACE INTO active_sessions (session_key, messages, last_activity, updated_at)
               VALUES (?, ?, ?, ?)""",
            (session_key, messages_json, last_activity, datetime.now(TZ).isoformat())
        )
        await db.commit()


async def load_active_sessions(timeout: float) -> list[tuple]:
    """Load sessions from DB that haven't expired.

    Returns list of (session_key, messages_json, last_activity) tuples.
    """
    cutoff = time.time() - timeout
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT session_key, messages, last_activity FROM active_sessions WHERE last_activity > ?",
            (cutoff,)
        )
        rows = await cursor.fetchall()
    return rows


async def clear_expired_sessions(timeout: float):
    """Delete expired sessions from DB."""
    cutoff = time.time() - timeout
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM active_sessions WHERE last_activity <= ?", (cutoff,))
        await db.commit()


def format_recent_context(messages: list[dict]) -> str:
    """Format recent messages as readable conversation context."""
    if not messages:
        return ""
    lines = []
    for msg in messages:
        prefix = "👤" if msg["role"] == "user" else "🤖"
        src = "[TG]" if msg["source"] == "telegram" else "[WA]"
        ts = msg.get("timestamp", "")[:16].split("T")[-1] if msg.get("timestamp") else "?"
        lines.append(f"[{ts}] {src} {prefix} {msg['user_name']}: {msg['text'][:300]}")
    return "\n".join(lines)


async def get_latest_summary() -> str | None:
    """Get the most recent conversation summary."""
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT summary, created_at FROM summaries ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                return row["summary"]
    except Exception as e:
        log.debug(f"Failed to load latest summary: {e}")
    return None


async def build_context_injection(limit: int = 20) -> str:
    """Build comprehensive context block for ephemeral task sessions.

    Gives the model full awareness of recent activity across both platforms,
    ongoing themes, and key context — without needing tool calls.

    Layers:
    1. SHORT-TERM: Recent messages (both TG and WA, with channel/time tags)
    2. LONG-TERM: Latest rolling summary (ongoing themes, decisions, action items)
    3. SEARCH HINT: Available tools for deeper context if needed
    """
    sections = []

    # 1. LONG-TERM: Latest rolling summary (themes, decisions, pending items)
    try:
        summary = await get_latest_summary()
        if summary:
            sections.append(
                "LONG-TERM CONTEXT (ongoing themes, decisions, action items):\n"
                + summary
            )
    except Exception as e:
        log.debug(f"Failed to load summary for context: {e}")

    # 2. SHORT-TERM: Recent messages across both platforms
    try:
        messages = await get_recent_messages(limit=limit)
        if messages:
            formatted = format_recent_context(messages)
            sections.append(
                f"SHORT-TERM CONTEXT (last {len(messages)} messages, both Telegram and WhatsApp):\n"
                + formatted
            )
    except Exception as e:
        log.debug(f"Failed to load recent messages for context: {e}")

    if not sections:
        return ""

    # 3. Search tools hint
    sections.append(
        "DEEPER CONTEXT AVAILABLE VIA TOOLS:\n"
        "If you need MORE context beyond what's above, use:\n"
        "- get_recent_conversation: load more messages from both platforms\n"
        "- search_memory: keyword search across all memory (messages, facts, knowledge, summaries, media)\n"
        "Only call these if the context above is insufficient for the current task."
    )

    return (
        "=== CONVERSATION CONTEXT ===\n"
        "(Auto-loaded for context awareness across Telegram and WhatsApp.)\n\n"
        + "\n\n".join(sections)
    )


# === SESSION SUMMARIZATION ===

async def get_unsummarized_message_count() -> int:
    """Count messages since the last summary was created."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        # Get the timestamp of the most recent summary
        cursor = await db.execute(
            "SELECT created_at FROM summaries ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row:
            last_summary_time = row[0]
            cursor2 = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE timestamp > ?",
                (last_summary_time,)
            )
        else:
            cursor2 = await db.execute("SELECT COUNT(*) FROM messages")
        count_row = await cursor2.fetchone()
        return count_row[0] if count_row else 0


async def get_messages_for_summary(limit: int = 30) -> list[dict]:
    """Get recent messages that haven't been summarized yet."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        # Get messages since last summary
        cursor = await db.execute(
            "SELECT created_at FROM summaries ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row:
            last_summary_time = row[0]
            cursor2 = await db.execute(
                "SELECT source, user_name, text, role, timestamp FROM messages "
                "WHERE timestamp > ? ORDER BY id ASC LIMIT ?",
                (last_summary_time, limit)
            )
        else:
            cursor2 = await db.execute(
                "SELECT source, user_name, text, role, timestamp FROM messages "
                "ORDER BY id DESC LIMIT ?",
                (limit,)
            )
        rows = await cursor2.fetchall()
    return [dict(r) for r in rows]


# === MEDIA CACHE ===

async def cache_media_file(
    source_path: str,
    media_type: str,
    source: str,
    sender_name: str = "",
    chat_id: str = "",
    original_filename: str = "",
    description: str = "",
    mime_type: str = "",
) -> dict:
    """Cache a media file: copy from temp to cache dir, index in SQLite.

    Args:
        source_path: path to the temporary downloaded file
        media_type: photo, document, voice, video, etc.
        source: telegram or whatsapp
        sender_name: who sent the media
        chat_id: chat/group ID
        original_filename: original filename from the sender
        description: caption or auto-description
        mime_type: MIME type if known

    Returns:
        dict with cached file info, or None on failure
    """
    src = Path(source_path)
    if not src.exists():
        log.warning(f"Media cache: source file not found: {source_path}")
        return None

    file_size = src.stat().st_size
    if file_size == 0:
        log.warning(f"Media cache: empty file skipped: {source_path}")
        return None

    # Generate a unique cached filename
    now = datetime.now(TZ)
    ts = now.strftime("%Y%m%d_%H%M%S")
    ext = src.suffix or _guess_extension(media_type)
    cached_filename = f"{ts}_{source}_{media_type}{ext}"
    cached_path = MEDIA_CACHE_DIR / cached_filename

    # Copy file to cache directory
    try:
        shutil.copy2(str(src), str(cached_path))
    except Exception as e:
        log.error(f"Media cache: failed to copy {src} -> {cached_path}: {e}")
        return None

    # Calculate expiry
    expires_at = (now + timedelta(days=MEDIA_RETENTION_DAYS)).isoformat()

    # Index in SQLite
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute(
                "INSERT INTO media_cache "
                "(filename, original_filename, media_type, source, chat_id, "
                " sender_name, description, file_size, mime_type, cached_at, expires_at, file_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cached_filename, original_filename or src.name, media_type, source,
                 chat_id, sender_name, description, file_size, mime_type,
                 now.isoformat(), expires_at, str(cached_path)),
            )
            await db.commit()
    except Exception as e:
        log.error(f"Media cache: failed to index {cached_filename}: {e}")
        # Clean up the copied file if DB insert fails
        cached_path.unlink(missing_ok=True)
        return None

    log.info(f"Media cached: {cached_filename} ({file_size} bytes, expires {expires_at[:10]})")
    return {
        "filename": cached_filename,
        "file_path": str(cached_path),
        "media_type": media_type,
        "file_size": file_size,
        "cached_at": now.isoformat(),
        "expires_at": expires_at,
    }


def _guess_extension(media_type: str) -> str:
    """Guess file extension from media type."""
    return {
        "photo": ".jpg",
        "document": "",
        "voice": ".ogg",
        "video": ".mp4",
        "video_note": ".mp4",
        "animation": ".mp4",
        "sticker": ".webp",
        "audio": ".mp3",
    }.get(media_type, "")


async def search_media_cache(query: str, limit: int = 10) -> list[dict]:
    """Search cached media files using FTS5."""
    safe_query = _sanitize_fts_query(query)
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT mc.filename, mc.original_filename, mc.media_type, mc.source, "
                "mc.sender_name, mc.description, mc.file_size, mc.cached_at, mc.file_path, "
                "rank "
                "FROM media_cache_fts f "
                "JOIN media_cache mc ON f.rowid = mc.id "
                "WHERE media_cache_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (safe_query, limit)
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"search_media_cache error: {e}")
        return []


async def list_cached_media(media_type: str = "", limit: int = 20) -> list[dict]:
    """List recently cached media files, optionally filtered by type."""
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            if media_type:
                cursor = await db.execute(
                    "SELECT filename, original_filename, media_type, source, sender_name, "
                    "description, file_size, cached_at, file_path "
                    "FROM media_cache WHERE media_type = ? "
                    "ORDER BY cached_at DESC LIMIT ?",
                    (media_type, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT filename, original_filename, media_type, source, sender_name, "
                    "description, file_size, cached_at, file_path "
                    "FROM media_cache ORDER BY cached_at DESC LIMIT ?",
                    (limit,),
                )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"list_cached_media error: {e}")
        return []


async def cleanup_expired_media():
    """Delete cached media files that have exceeded the retention period.

    Called periodically from the scheduler loop.
    """
    now = datetime.now(TZ).isoformat()
    deleted_count = 0
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            # Find expired entries
            cursor = await db.execute(
                "SELECT id, filename, file_path FROM media_cache WHERE expires_at < ?",
                (now,),
            )
            expired = await cursor.fetchall()

            for row in expired:
                row_id, filename, file_path = row
                # Delete the file
                fpath = Path(file_path)
                if fpath.exists():
                    try:
                        fpath.unlink()
                        deleted_count += 1
                    except Exception as e:
                        log.warning(f"Failed to delete expired media file {filename}: {e}")
                else:
                    deleted_count += 1  # File already gone, still clean up DB

                # Remove FTS entry first (required for content= tables)
                await db.execute(
                    "INSERT INTO media_cache_fts(media_cache_fts, rowid, filename, original_filename, "
                    "description, sender_name, media_type) "
                    "SELECT 'delete', id, filename, original_filename, description, sender_name, media_type "
                    "FROM media_cache WHERE id = ?",
                    (row_id,),
                )

            # Delete from main table
            if expired:
                await db.execute(
                    "DELETE FROM media_cache WHERE expires_at < ?",
                    (now,),
                )
                await db.commit()

        if deleted_count:
            log.info(f"Media cache cleanup: deleted {deleted_count} expired files")
    except Exception as e:
        log.error(f"Media cache cleanup error: {e}")


async def get_media_cache_stats() -> dict:
    """Get statistics about the media cache."""
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM media_cache")
            total_files = (await cursor.fetchone())[0]

            cursor = await db.execute("SELECT SUM(file_size) FROM media_cache")
            total_size = (await cursor.fetchone())[0] or 0

            cursor = await db.execute(
                "SELECT media_type, COUNT(*) FROM media_cache GROUP BY media_type"
            )
            by_type = {row[0]: row[1] for row in await cursor.fetchall()}

        return {
            "total_files": total_files,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "by_type": by_type,
            "retention_days": MEDIA_RETENTION_DAYS,
            "cache_dir": str(MEDIA_CACHE_DIR),
        }
    except Exception as e:
        log.error(f"get_media_cache_stats error: {e}")
        return {"error": str(e)}
