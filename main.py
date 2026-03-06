"""Family Bot — FastAPI application with Claude Agent SDK.

Supports parallel task processing (up to MAX_PARALLEL_TASKS concurrent),
real-time TG streaming (thinking + tools + text in one placeholder),
WA batch status updates once a minute, smart cancel, and self-diagnostics.
"""

import asyncio
import logging
import logging.handlers
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load .env before importing config
load_dotenv()

from fastapi import FastAPI, Request, Response
import uvicorn

from config import (
    HOST, PORT, TG_WEBHOOK_URL, TG_CHAT_ID,
    DATA_DIR, CLAUDE_CODE_OAUTH_TOKEN,
    WA_DB_PATH, WA_POLL_INTERVAL,
    WA_ALLOWED_PHONES, WA_ALLOWED_CHATS, WA_FAMILY_GROUP_JID,
    TG_ALLOWED_USERS,
    QUICK_TIMEOUT, LONG_STATUS_INTERVAL, LONG_SOFT_TIMEOUT,
    STREAM_EDIT_INTERVAL, MAX_PARALLEL_TASKS,
    MODEL_QUICK, MODEL_LONG,
    FAMILY_TIMEZONE,
)

TZ = ZoneInfo(FAMILY_TIMEZONE)

# Git commit hash (for /health endpoint)
GIT_COMMIT = os.environ.get("GIT_COMMIT", "")
if not GIT_COMMIT:
    try:
        head = Path("/host-repo/.git/HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref_path = Path("/host-repo/.git") / head[5:]
            GIT_COMMIT = ref_path.read_text().strip()[:7]
        else:
            GIT_COMMIT = head[:7]
    except Exception:
        GIT_COMMIT = "unknown"

# === LOGGING (with rotation: 10MB max, 5 backups) ===
LOG_DIR = Path(os.environ.get("LOG_DIR", DATA_DIR / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_file_handler = logging.handlers.RotatingFileHandler(
    str(LOG_DIR / "bot.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(_log_formatter)
_file_handler.setLevel(logging.DEBUG)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_log_formatter)
_console_handler.setLevel(logging.INFO)

logging.basicConfig(
    level=logging.DEBUG,
    handlers=[_file_handler, _console_handler],
)

# Silence noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

# === STATE FILES ===
STARTUP_FILE = DATA_DIR / "last_startup.txt"
WA_POLL_FILE = DATA_DIR / "wa_last_poll.txt"

# Track bot startup time — messages before this are stale (context-only, not actionable)
_BOT_STARTUP_TIME: float = time.time()


# === STARTUP VALIDATION ===

def validate_startup():
    """Check critical config before starting."""
    from config import TG_BOT_TOKEN
    creds_file = Path.home() / ".claude" / ".credentials.json"
    if creds_file.exists():
        log.info("  Auth: file-based credentials (~/.claude/.credentials.json) — auto-refresh enabled")
    elif CLAUDE_CODE_OAUTH_TOKEN:
        log.info("  Auth: CLAUDE_CODE_OAUTH_TOKEN env var (no auto-refresh)")
    else:
        log.info("  Auth: No credentials found!")
        log.info("  Run scripts/refresh_server_token.sh, or: docker exec -it <container> su -c 'claude auth login' botuser")

    if not TG_BOT_TOKEN:
        log.error("TG_BOT_TOKEN is not set!")
        sys.exit(1)
    log.info(f"  [TG] Bot token: set (bot ID: {TG_BOT_TOKEN.split(':')[0]})")
    log.info(f"  [TG] Webhook URL: {TG_WEBHOOK_URL or 'NOT SET'}")
    log.info(f"  [TG] Chat ID: {TG_CHAT_ID}")

    if not WA_DB_PATH.exists():
        log.warning(f"  [WA] Bridge DB not found at {WA_DB_PATH}")
    else:
        log.info(f"  [WA] Bridge DB: {WA_DB_PATH}")
    log.info(f"  [WA] Family group JID: {WA_FAMILY_GROUP_JID}")

    from config import GOOGLE_WORKSPACE_CREDS_DIR
    cred_files = list(GOOGLE_WORKSPACE_CREDS_DIR.glob("*.json"))
    if not cred_files:
        log.warning("  [Google] No workspace-mcp credentials found")
    else:
        log.info(f"  [Google] Workspace MCP credentials: {[f.name for f in cred_files]}")

    from config import VAPI_API_KEY, VAPI_PHONE_NUMBER_ID
    if VAPI_API_KEY and VAPI_PHONE_NUMBER_ID:
        log.info("  [Phone] Vapi configured")
    else:
        log.warning("  [Phone] Vapi not configured")

    try:
        import claude_agent_sdk  # noqa: F401
        log.info(f"  [SDK] Claude Agent SDK available")
    except ImportError:
        log.error("  [SDK] Claude Agent SDK NOT installed!")
        sys.exit(1)


# === SCHEDULED TASKS ===

async def startup_catchup():
    """Run catchup if bot was offline for a while."""
    try:
        last_str = STARTUP_FILE.read_text().strip()
        last_active = datetime.fromisoformat(last_str)
        if last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=TZ)
        gap_hours = (datetime.now(TZ) - last_active).total_seconds() / 3600
    except (FileNotFoundError, ValueError):
        gap_hours = 24

    STARTUP_FILE.write_text(datetime.now(TZ).isoformat())

    if gap_hours < 0.25:
        log.info(f"Short gap ({gap_hours:.1f}h) — no catchup needed")
        return

    log.info(f"Detected downtime gap: {gap_hours:.1f} hours — running catchup")
    from bot.agent import process_system_task
    from bot.prompts import build_catchup_prompt

    try:
        await process_system_task(build_catchup_prompt(gap_hours))
        # Mark email_check task as run to avoid duplicate check right after catchup
        from bot.scheduler import mark_task_run
        mark_task_run("email_check")
        log.info("Startup catchup complete")
    except Exception as e:
        log.error(f"Startup catchup failed: {e}")


async def cleanup_tmp_files():
    """Remove temp files older than 1 hour."""
    try:
        now_ts = time.time()
        tmp_dir = DATA_DIR / "tmp"
        if not tmp_dir.exists():
            return
        for fpath in tmp_dir.iterdir():
            if fpath.is_file() and (now_ts - fpath.stat().st_mtime) > 3600:
                fpath.unlink(missing_ok=True)
                log.info(f"Cleaned up temp file: {fpath.name}")
    except Exception as e:
        log.warning(f"Temp cleanup error: {e}")


async def cleanup_expired_media():
    """Remove cached media files that have exceeded the retention period."""
    try:
        from bot.memory import cleanup_expired_media as do_cleanup
        await do_cleanup()
    except Exception as e:
        log.warning(f"Media cache cleanup error: {e}")


async def maybe_auto_summarize():
    """Generate a conversation summary if enough unsummarized messages have accumulated."""
    try:
        from bot.memory import (
            get_unsummarized_message_count, get_messages_for_summary,
            format_recent_context, store_summary,
        )
        count = await get_unsummarized_message_count()
        if count < 20:
            return

        log.info(f"Auto-summarize: {count} unsummarized messages, generating summary...")
        messages = await get_messages_for_summary(limit=30)
        if not messages:
            return

        formatted = format_recent_context(messages)

        # Use a lightweight Sonnet call for summarization
        from bot.agent import process_system_task
        from config import MODEL_QUICK
        summary_prompt = (
            f"[INTERNAL_SUMMARIZE] Create a structured conversation summary for AI context injection. "
            f"This summary will be auto-injected into future task sessions to provide continuity.\n\n"
            f"Include these sections (only if relevant content exists):\n"
            f"• TOPICS: Key topics discussed and their outcomes (1-2 lines each)\n"
            f"• DECISIONS: Any decisions made or preferences expressed\n"
            f"• PENDING: Unresolved questions, action items waiting for follow-up\n"
            f"• FACTS LEARNED: New information about the family, schedule, preferences\n"
            f"• THREADS: Ongoing topics that may come up again\n\n"
            f"Be concise (5-12 bullet points total). Use [TG] and [WA] tags to note which platform.\n"
            f"Do NOT send any messages to Telegram or WhatsApp. Just return the summary text.\n\n"
            f"CONVERSATION TO SUMMARIZE:\n{formatted}"
        )

        result = await process_system_task(summary_prompt, model=MODEL_QUICK)
        if result and len(result) > 20:
            await store_summary("auto", result)
            log.info(f"Auto-summary stored ({len(result)} chars)")
        else:
            log.warning(f"Auto-summarize returned empty/short result")
    except Exception as e:
        log.warning(f"Auto-summarize failed: {e}")


async def check_custom_tasks():
    """Check and execute any due custom scheduled tasks."""
    from bot.scheduler import get_due_tasks, mark_task_run
    from bot.agent import process_system_task
    from bot.prompts import build_scheduled_task_prompt

    due_tasks = get_due_tasks()
    for task in due_tasks:
        task_id = task["id"]
        task_name = task["name"]
        platform = task.get("platform", "telegram")
        log.info(f">>> Firing scheduled task: {task_name} ({task_id})")
        try:
            prompt = build_scheduled_task_prompt(task_name, task["prompt"], platform)
            await process_system_task(prompt)
            mark_task_run(task_id)
            log.info(f"Scheduled task completed: {task_name}")
        except Exception as e:
            log.error(f"Scheduled task failed ({task_name}): {e}")
            mark_task_run(task_id)  # Mark as run to avoid retry loop


async def scheduler_loop():
    """Background loop for periodic tasks."""
    log.info("Scheduler started")
    cleanup_counter = 0
    while True:
        try:
            STARTUP_FILE.write_text(datetime.now(TZ).isoformat())
            await check_custom_tasks()
            cleanup_counter += 1
            # Cleanup expired sessions every ~10 min (20 x 30s ticks)
            if cleanup_counter % 20 == 0:
                await cleanup_tmp_files()
                from bot.agent import client_pool
                await client_pool.cleanup_expired()
            # Resource watchdog every ~5 min (10 x 30s ticks)
            if cleanup_counter % 10 == 0:
                from bot.agent import client_pool
                await client_pool.resource_watchdog()
            # Auto-summarize every ~5 min (check if enough messages accumulated)
            if cleanup_counter % 10 == 5:
                await maybe_auto_summarize()
            # RAG incremental chunk update every ~5 min (offset from summarize)
            if cleanup_counter % 10 == 8:
                try:
                    from bot.rag import update_chunks_incremental
                    await update_chunks_incremental()
                except Exception as e:
                    log.warning(f"RAG incremental update failed: {e}")
            if cleanup_counter >= 60:
                cleanup_counter = 0
                await cleanup_expired_media()
        except Exception as e:
            log.error(f"Scheduler error: {e}")
        await asyncio.sleep(30)


# === WHATSAPP POLLING ===

async def wa_polling_loop():
    """Poll the WhatsApp bridge SQLite DB for new incoming messages."""
    log.info("WhatsApp polling started")

    try:
        last_ts = WA_POLL_FILE.read_text().strip().replace("T", " ")
    except FileNotFoundError:
        last_ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S%z")
        WA_POLL_FILE.write_text(last_ts)

    while True:
        try:
            if not WA_DB_PATH.exists():
                await asyncio.sleep(WA_POLL_INTERVAL)
                continue

            from integrations.whatsapp import get_new_messages_since
            new_messages = await get_new_messages_since(last_ts)

            for msg in new_messages:
                if msg["chat_jid"] not in WA_ALLOWED_CHATS:
                    last_ts = msg["timestamp"]
                    WA_POLL_FILE.write_text(last_ts)
                    continue

                # SECURITY: Hard filter — only authorized phone numbers get processed
                sender_phone = msg.get("phone", "")
                if sender_phone and sender_phone not in WA_ALLOWED_PHONES:
                    log.warning(f"WA BLOCKED unauthorized sender {sender_phone} "
                                f"({msg.get('sender_name', '?')}): {msg.get('content', '')[:60]}")
                    last_ts = msg["timestamp"]
                    WA_POLL_FILE.write_text(last_ts)
                    continue

                # Check if message is stale (arrived before bot startup)
                # Parse message timestamp and compare to startup time
                msg_ts = msg["timestamp"]
                is_stale = False
                try:
                    msg_dt = datetime.fromisoformat(msg_ts.replace(" ", "T"))
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=TZ)
                    is_stale = msg_dt.timestamp() < _BOT_STARTUP_TIME
                except (ValueError, TypeError):
                    pass

                if is_stale:
                    # Store for context but don't dispatch as a task
                    log.info(f"WA stale message (pre-startup), storing for context: "
                             f"{msg['sender_name']}: {msg['content'][:60]}")
                    try:
                        from bot.memory import store_message
                        await store_message(
                            "whatsapp", msg["sender_name"],
                            msg["content"] or "[Media]", "user",
                            msg["phone"], msg["id"],
                        )
                    except Exception:
                        pass
                    last_ts = msg_ts
                    WA_POLL_FILE.write_text(last_ts)
                    continue

                log.info(f"WA message from {msg['sender_name']}: {msg['content'][:80]}")
                await _message_queue.put({
                    "source": "whatsapp",
                    "user_name": msg["sender_name"],
                    "user_id": msg["phone"],
                    "text": msg["content"] or "[Media/No text]",
                    "message_id": msg["id"],
                    "chat_jid": msg["chat_jid"],
                    "has_media": bool(msg.get("media_type")),
                    "raw": msg,
                })
                last_ts = msg_ts
                WA_POLL_FILE.write_text(last_ts)

        except Exception as e:
            log.error(f"WA polling error: {e}")
        await asyncio.sleep(WA_POLL_INTERVAL)


# === TOOL STATUS LABELS ===

_TOOL_LABELS = {
    # SDK built-in tools
    "WebSearch": "Searching web",
    "WebFetch": "Fetching page",
    # Google Workspace MCP tools
    "search_gmail_messages": "Checking email",
    "get_gmail_message_content": "Reading email",
    "send_gmail_message": "Sending email",
    "get_gmail_attachment_content": "Downloading attachment",
    "get_events": "Checking calendar",
    "manage_event": "Managing event",
    "search_memory": "Searching memory",
    "get_recent_conversation": "Loading history",
    "phone_call": "Making call",
    # Playwright MCP tools
    "browser_navigate": "Opening browser",
    "browser_snapshot": "Reading page",
    "browser_screenshot": "Taking screenshot",
    # WhatsApp MCP tools
    "list_messages": "Reading WhatsApp",
    "search_contacts": "Searching contacts",
    # SDK built-in file tools
    "Read": "Reading file",
    "Write": "Writing file",
    "Edit": "Editing file",
    "Bash": "Running command",
    "Grep": "Searching code",
    "Glob": "Finding files",
}

_SEND_TOOLS = frozenset({
    "telegram_send_message", "telegram_send_photo", "telegram_send_document",
    "whatsapp_send_message", "whatsapp_send_file",  # Custom WA tools
    "send_message", "send_file",  # External WhatsApp MCP tools (bare names)
})


# === TASK MANAGER (parallel processing) ===

@dataclass
class ActiveTask:
    """Tracks a single in-flight task across platforms."""
    task_id: str
    source: str
    chat_id: str
    user_name: str
    user_id: str
    text: str
    started_at: float = field(default_factory=time.time)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # TG-specific
    tg_placeholder_id: int | None = None
    tg_placeholder_alive: bool = True
    tg_last_edit: float = 0.0
    tg_last_status_text: str = ""
    tg_reply_sent: bool = False
    # Status tracking
    phase: str = "thinking"  # thinking, tools, streaming, done, failed
    tools_used: list[str] = field(default_factory=list)
    tool_labels_seen: list[str] = field(default_factory=list)
    streaming_text: str = ""
    error: str = ""
    _asyncio_task: asyncio.Task | None = field(default=None, repr=False)

    def elapsed(self) -> str:
        s = int(time.time() - self.started_at)
        if s < 60:
            return f"{s}s"
        return f"{s // 60}m{s % 60:02d}s"

    def elapsed_seconds(self) -> float:
        return time.time() - self.started_at


class TaskManager:
    """Manages parallel tasks with real-time status across TG and WA."""

    def __init__(self):
        self._tasks: dict[str, ActiveTask] = {}  # task_id -> ActiveTask
        self._wa_last_batch_update: float = 0.0

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def get_active_tasks(self) -> list[ActiveTask]:
        return list(self._tasks.values())

    def get_task(self, task_id: str) -> ActiveTask | None:
        return self._tasks.get(task_id)

    def can_accept_task(self) -> bool:
        return self.active_count < MAX_PARALLEL_TASKS

    def other_tasks_summary(self, exclude_id: str) -> str:
        """Build a brief summary of other active tasks for context awareness."""
        others = [t for t in self._tasks.values() if t.task_id != exclude_id]
        if not others:
            return ""
        lines = ["OTHER ACTIVE TASKS (for context awareness):"]
        for t in others:
            status = f"{t.phase}"
            if t.tool_labels_seen:
                status = t.tool_labels_seen[-1]
            lines.append(f"  - [{t.elapsed()}] {t.text[:80]} ({status})")
        return "\n".join(lines)

    def register_task(self, source: str, chat_id: str, user_name: str,
                      user_id: str, text: str) -> ActiveTask:
        task_id = str(uuid.uuid4())[:8]
        task = ActiveTask(
            task_id=task_id,
            source=source,
            chat_id=chat_id,
            user_name=user_name,
            user_id=user_id,
            text=text[:200],
        )
        self._tasks[task_id] = task
        log.info(f"Task {task_id} registered: {text[:60]} (active={self.active_count})")
        return task

    def complete_task(self, task_id: str):
        task = self._tasks.pop(task_id, None)
        if task:
            task.phase = "done"
            log.info(f"Task {task_id} completed ({task.elapsed()}, tools={task.tools_used})")

    def fail_task(self, task_id: str, error: str):
        task = self._tasks.get(task_id)
        if task:
            task.phase = "failed"
            task.error = error
        self._tasks.pop(task_id, None)
        log.error(f"Task {task_id} failed: {error[:100]}")

    def find_cancel_target(self, text: str) -> ActiveTask | None:
        """Find the task that a cancel message refers to.

        Handles: "cancel", "stop", exact cancel, "cancel this task and ...",
        cancel by keyword match.
        """
        # If only one task, cancel it
        active = self.get_active_tasks()
        if len(active) == 1:
            return active[0]
        if not active:
            return None
        # Try to match by keyword from the cancel message
        text_lower = text.lower()
        for word in ["cancel", "stop", "abort", "kill"]:
            text_lower = text_lower.replace(word, "").strip()
        # Remove common phrases
        for phrase in ["this task", "that task", "the task", "and check",
                       "and look at", "status", "logs", "please"]:
            text_lower = text_lower.replace(phrase, "").strip()
        # If no identifying text left, cancel the oldest
        if not text_lower or len(text_lower) < 3:
            return min(active, key=lambda t: t.started_at)
        # Try fuzzy match against task text
        for task in active:
            if any(w in task.text.lower() for w in text_lower.split() if len(w) > 2):
                return task
        # Default: cancel oldest
        return min(active, key=lambda t: t.started_at)


task_manager = TaskManager()


# === TG STREAMING (real-time thinking + tools + text in one placeholder) ===

async def _tg_safe_edit(task: ActiveTask, new_text: str, chat_id: int | str = None,
                        parse_mode: str | None = None):
    """Edit a TG placeholder message, handling errors gracefully."""
    if not task.tg_placeholder_id or not task.tg_placeholder_alive:
        return
    if new_text == task.tg_last_status_text:
        return
    task.tg_last_status_text = new_text
    try:
        from integrations.telegram import edit_message
        cid = chat_id or int(task.chat_id)
        await edit_message(task.tg_placeholder_id, new_text, chat_id=cid,
                           parse_mode=parse_mode)
    except Exception:
        task.tg_placeholder_alive = False


def _animated_dots(task: ActiveTask) -> str:
    """Return animated dots based on elapsed time."""
    n = int(task.elapsed_seconds()) % 4
    return "." * (n + 1)


def _build_tg_status(task: ActiveTask) -> str:
    """Build a combined TG placeholder showing thinking + tools + streamed text."""
    elapsed = task.elapsed()
    dots = _animated_dots(task)

    if task.phase == "streaming":
        return task.streaming_text[:4000]
    elif task.phase == "failed":
        return f"⚠️ Error: {task.error[:200]}"

    # Show tools chain if we have any
    if task.tool_labels_seen:
        recent = task.tool_labels_seen[-5:]
        tools_line = " → ".join(recent)
        return f"🔧 {tools_line}{dots}  ({elapsed})"

    if task.phase == "thinking":
        return f"🧠 Thinking{dots} ({elapsed})"

    return f"🧠 Processing{dots} ({elapsed})"


async def _tg_ticker(task: ActiveTask):
    """Background ticker: update TG placeholder every 1s with current status."""
    tick = 0
    while not task.cancel_event.is_set() and task.phase not in ("done", "failed"):
        await asyncio.sleep(1)
        if task.tg_reply_sent or task.phase == "streaming":
            break
        if task.phase in ("done", "failed"):
            break
        tick += 1
        # Only edit every 5s to avoid Telegram rate limits
        if tick % 5 != 0:
            continue
        status = _build_tg_status(task)
        await _tg_safe_edit(task, status)


# === WA BATCH STATUS ===

async def _wa_batch_ticker():
    """Background loop: send combined WA status for all active tasks once a minute."""
    while True:
        await asyncio.sleep(30)
        active = task_manager.get_active_tasks()
        wa_tasks = [t for t in active if t.source == "whatsapp"
                    and t.phase not in ("done", "failed")
                    and not t.tg_reply_sent]  # reuse flag for WA send-tool detection
        if not wa_tasks:
            continue

        now = time.time()
        if now - task_manager._wa_last_batch_update < 55:
            continue
        task_manager._wa_last_batch_update = now

        # Build batch status
        if len(wa_tasks) == 1:
            t = wa_tasks[0]
            elapsed = t.elapsed()
            if t.tool_labels_seen:
                status = t.tool_labels_seen[-1]
            else:
                status = "Thinking..."
            msg = f"🔧 {status} ({elapsed})"
        else:
            lines = []
            for t in wa_tasks:
                elapsed = t.elapsed()
                if t.tool_labels_seen:
                    status = t.tool_labels_seen[-1]
                elif t.phase == "thinking":
                    status = "Thinking..."
                else:
                    status = t.phase.capitalize() + "..."
                lines.append(f"• {t.text[:60]} — {status} ({elapsed})")
            msg = "\n".join(lines)

        try:
            from integrations.whatsapp import send_message as wa_send
            # Send to the first task's chat (they're all in the same family group)
            await wa_send(wa_tasks[0].chat_id, msg)
        except Exception as e:
            log.warning(f"WA batch status failed: {e}")


# === MESSAGE PROCESSING (with parallel tasks) ===

_message_queue: asyncio.Queue = asyncio.Queue()


async def handle_telegram_message(parsed: dict, task: ActiveTask):
    """Process a TG message with real-time streaming in the task's placeholder."""
    from bot.agent import process_incoming
    from integrations.telegram import (
        send_chat_action, send_message, edit_message, delete_message, download_media,
    )

    user_name = parsed["user_name"]
    user_id = str(parsed["user_id"])
    text = parsed["text"]
    message_id = str(parsed.get("message_id", ""))
    chat_id = parsed.get("chat_id", TG_CHAT_ID)

    # Enrich text with reply/forward/location context
    reply_to = parsed.get("reply_to")
    if reply_to:
        parts = []
        if reply_to.get("text"):
            label = "bot's previous response" if reply_to.get("is_bot") else "earlier message"
            parts.append(f'[Replying to {label}: "{reply_to["text"][:300]}"]')
        if reply_to.get("location"):
            loc = reply_to["location"]
            coords = f"{loc.get('latitude')}, {loc.get('longitude')}"
            loc_type = "live location" if loc.get("live_period") else "location"
            if loc.get("title"):
                addr = f", {loc['address']}" if loc.get("address") else ""
                parts.append(f"[Replying to {loc_type}: {loc['title']}{addr} ({coords})]")
            else:
                parts.append(f"[Replying to {loc_type}: {coords}]")
        if reply_to.get("contact"):
            c = reply_to["contact"]
            name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
            phone = c.get("phone_number", "")
            parts.append(f"[Replying to shared contact: {name}" + (f", {phone}]" if phone else "]"))
        if reply_to.get("has_media"):
            parts.append("[Replying to message with media — see attached]")
        if parts:
            text = "\n".join(parts) + "\n" + text

    forward = parsed.get("forward")
    if forward:
        fwd_name = forward.get("sender_name", "someone")
        # For forwarded messages, the user's own text is empty — only forwarded content exists.
        # Ask what to do instead of blindly processing.
        text = (f"[Forwarded message from {fwd_name}]\n{text}\n\n"
                f"[SYSTEM: This is a forwarded message. The user did not add their own instructions. "
                f"Briefly acknowledge the content and ask what they'd like you to do with it.]")

    # Direct location message
    if parsed.get("location"):
        loc = parsed["location"]
        coords = f"{loc.get('latitude')}, {loc.get('longitude')}"
        if loc.get("title"):
            addr = f", {loc['address']}" if loc.get("address") else ""
            loc_desc = f"{loc['title']}{addr} ({coords})"
        else:
            loc_desc = coords
        loc_type = "Live location" if loc.get("live_period") else "Shared location"
        text = f"[{loc_type}: {loc_desc}]\n{text}" if text else f"[{loc_type}: {loc_desc}]"

    # Direct contact message
    if parsed.get("contact"):
        c = parsed["contact"]
        name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
        phone = c.get("phone_number", "")
        contact_desc = f"{name}" + (f", {phone}" if phone else "")
        text = f"[Shared contact: {contact_desc}]\n{text}" if text else f"[Shared contact: {contact_desc}]"

    # Download media — from direct message, reply-to, or forwarded message
    media_info = None
    if parsed.get("has_media"):
        media_info = await download_media(parsed["raw"])
    elif reply_to and reply_to.get("has_media") and reply_to.get("raw"):
        media_info = await download_media(reply_to["raw"])
        if media_info:
            log.info(f"Downloaded media from reply-to: {media_info.get('media_type')}")

    # Send placeholder
    await send_chat_action("typing", chat_id=chat_id)
    placeholder_msg = await send_message("🧠 Thinking...", chat_id=chat_id, parse_mode=None)
    if placeholder_msg:
        task.tg_placeholder_id = placeholder_msg["message_id"]

    # Start TG ticker for this task
    ticker = asyncio.create_task(_tg_ticker(task))

    async def on_thinking_start():
        task.phase = "thinking"
        await _tg_safe_edit(task, f"🧠 Thinking... ({task.elapsed()})")

    async def on_tool_status(tool_names: list[str]):
        for n in tool_names:
            if n in _SEND_TOOLS:
                task.tg_reply_sent = True
                return
        if task.phase == "streaming" or task.tg_reply_sent:
            return
        labels = [_TOOL_LABELS.get(n, n) for n in tool_names
                  if n not in _SEND_TOOLS]
        if labels:
            task.tool_labels_seen.extend(labels)
            task.tools_used.extend(tool_names)
            task.phase = "tools"
            status = _build_tg_status(task)
            await _tg_safe_edit(task, status)

    async def on_stream_chunk(text_so_far: str):
        if len(text_so_far) <= 5:
            return
        task.streaming_text = text_so_far
        task.phase = "streaming"
        now = time.time()
        if now - task.tg_last_edit < STREAM_EDIT_INTERVAL:
            return
        task.tg_last_edit = now
        await _tg_safe_edit(task, text_so_far[:4000])

    # Build context about other parallel tasks
    other_ctx = task_manager.other_tasks_summary(task.task_id)

    try:
        await process_incoming(
            source="telegram",
            user_name=user_name,
            user_id=user_id,
            text=text,
            message_id=message_id,
            media_info=media_info,
            on_stream_chunk=on_stream_chunk,
            on_thinking_start=on_thinking_start,
            on_tool_status=on_tool_status,
            chat_id=str(chat_id),
            model=parsed.get("model", MODEL_LONG),
            task_id=task.task_id,
            other_tasks_context=other_ctx,
        )
        # Delete placeholder if bot replied via send tool (its own message replaces placeholder)
        if (task.tg_placeholder_id and task.tg_placeholder_alive
                and task.tg_reply_sent):
            await delete_message(task.tg_placeholder_id, chat_id=chat_id)
    except Exception as e:
        log.error(f"TG message processing failed: {e}", exc_info=True)
        if task.tg_placeholder_id and task.tg_placeholder_alive:
            await _tg_safe_edit(task, f"⚠️ Error: {str(e)[:200]}")
            # Suggest self-diagnose on failure
            await _suggest_diagnose_on_failure(task, str(e))
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        # Clean up placeholder if task finished without producing visible output
        from bot.hooks import intentional_silence as _silence_var
        is_silent = _silence_var.get(False)
        if task.tg_placeholder_id and task.tg_placeholder_alive and not task.tg_reply_sent:
            if is_silent:
                # Intentional silence (e.g. tagged another user) — delete placeholder
                await delete_message(task.tg_placeholder_id, chat_id=chat_id)
            elif task.phase != "streaming":
                if task.tools_used:
                    # Tools ran but no reply — likely a real failure
                    elapsed = task.elapsed()
                    tools_summary = ", ".join(task.tools_used[-5:])
                    await _tg_safe_edit(task,
                        f"⚠️ Task finished without reply ({elapsed}, tools: {tools_summary}).\n"
                        f"Send \"diagnose\" to run self-diagnostics.")
                    await _suggest_diagnose_on_failure(task, "Task produced no output")
                else:
                    # No tools, no reply, no silence flag — delete placeholder
                    await delete_message(task.tg_placeholder_id, chat_id=chat_id)
        if media_info and media_info.get("local_path"):
            try:
                from bot.memory import cache_media_file
                cached = await cache_media_file(
                    source_path=media_info["local_path"],
                    media_type=media_info.get("media_type", "unknown"),
                    source="telegram",
                    sender_name=user_name,
                    chat_id=str(chat_id),
                    original_filename=media_info.get("filename", ""),
                    description=text or "",
                    mime_type=media_info.get("mime_type", ""),
                )
                if cached:
                    log.info(f"Media cached: {cached['filename']}")
                media_path = Path(media_info["local_path"])
                if media_path.exists():
                    media_path.unlink()
            except Exception as e:
                log.warning(f"Failed to cache/clean up media file: {e}")


async def handle_whatsapp_message(parsed: dict, task: ActiveTask):
    """Process a WA message. Status updates handled by _wa_batch_ticker."""
    from bot.agent import process_incoming

    user_name = parsed["user_name"]
    user_id = parsed["user_id"]
    text = parsed["text"]
    message_id = parsed.get("message_id", "")
    chat_jid = parsed.get("chat_jid", WA_FAMILY_GROUP_JID)

    # Download WA media if present
    media_info = None
    if parsed.get("has_media"):
        raw = parsed.get("raw", {})
        wa_media_type = raw.get("media_type", "")
        if wa_media_type:
            try:
                from integrations.whatsapp import download_media
                dl_result = await download_media(message_id, chat_jid)
                if dl_result.get("success"):
                    media_info = {
                        "local_path": dl_result.get("path", ""),
                        "media_type": wa_media_type,
                        "filename": dl_result.get("filename", ""),
                    }
            except Exception as e:
                log.warning(f"WA media download failed: {e}")

    # Send initial "Thinking..." to WA
    try:
        from integrations.whatsapp import send_message as wa_send
        await wa_send(chat_jid, f"🧠 Thinking...")
    except Exception:
        pass

    async def wa_tool_status(tool_names: list[str]):
        """Collect tool labels for WA batch status updates."""
        for n in tool_names:
            if n in _SEND_TOOLS:
                task.tg_reply_sent = True  # reuse flag
                return
        labels = [_TOOL_LABELS.get(n, n) for n in tool_names
                  if n not in _SEND_TOOLS]
        if labels:
            task.tool_labels_seen.extend(labels)
            task.tools_used.extend(tool_names)
            task.phase = "tools"

    # Build context about other parallel tasks
    other_ctx = task_manager.other_tasks_summary(task.task_id)

    try:
        await process_incoming(
            source="whatsapp",
            user_name=user_name,
            user_id=user_id,
            text=text,
            message_id=message_id,
            media_info=media_info,
            on_tool_status=wa_tool_status,
            chat_id=chat_jid,
            model=parsed.get("model", MODEL_LONG),
            task_id=task.task_id,
            other_tasks_context=other_ctx,
        )
    except Exception as e:
        log.error(f"WA message processing failed: {e}")
        try:
            from integrations.whatsapp import send_message
            await send_message(chat_jid, f"⚠️ Error: {str(e)[:200]}")
            await _suggest_diagnose_on_failure(task, str(e))
        except Exception:
            pass
    finally:
        if media_info and media_info.get("local_path"):
            try:
                from bot.memory import cache_media_file
                cached = await cache_media_file(
                    source_path=media_info["local_path"],
                    media_type=media_info.get("media_type", "unknown"),
                    source="whatsapp",
                    sender_name=user_name,
                    chat_id=chat_jid,
                    original_filename=media_info.get("filename", ""),
                    description=text or "",
                )
                if cached:
                    log.info(f"WA media cached: {cached['filename']}")
                media_path = Path(media_info["local_path"])
                if media_path.exists():
                    media_path.unlink()
            except Exception as e:
                log.warning(f"Failed to cache WA media: {e}")


# === 10-MINUTE CHECKPOINT ===

async def _task_checkpoint_loop():
    """Check active tasks every 30s; after 10 min, suggest continue/cancel."""
    while True:
        await asyncio.sleep(30)
        for task in task_manager.get_active_tasks():
            elapsed = task.elapsed_seconds()
            # At 10 minutes, send a checkpoint message
            if 595 < elapsed < 635 and task.phase not in ("done", "failed"):
                msg = (
                    f"⏳ This has been running for {task.elapsed()} — "
                    f"tools used: {', '.join(task.tools_used[-5:]) or 'none yet'}.\n"
                    f"Send \"continue\" to keep going, \"cancel\" to stop, "
                    f"or \"diagnose\" to check what's wrong."
                )
                await _send_to_platform_from_task(task, msg)
            # At 15 minutes, warn more strongly
            elif 895 < elapsed < 935 and task.phase not in ("done", "failed"):
                msg = (
                    f"⚠️ Task has been running for {task.elapsed()}. "
                    f"This may indicate a problem. Send \"cancel\" to stop, "
                    f"or \"diagnose\" to run self-diagnostics."
                )
                await _send_to_platform_from_task(task, msg)


async def _send_to_platform_from_task(task: ActiveTask, msg: str):
    """Send a message on the task's platform."""
    try:
        if task.source == "whatsapp":
            from integrations.whatsapp import send_message
            await send_message(task.chat_id, msg)
        else:
            from integrations.telegram import send_message
            await send_message(msg, chat_id=int(task.chat_id))
    except Exception:
        pass


# === SELF-DIAGNOSE ===

_DIAGNOSE_RE = re.compile(
    r"^(?:diagnose|self-diagnose|диагностика|check health|check bot|bot status|"
    r"what'?s wrong|что случилось|проверь бота)[\s?!.]*$",
    re.IGNORECASE,
)


async def run_self_diagnose(source: str, chat_id: str):
    """Run self-diagnostics using a clean ephemeral session."""
    log.info("Running self-diagnosis...")
    from bot.agent import process_system_task

    # Gather diagnostic info
    diagnostics = []

    # 1. Recent logs
    try:
        log_path = LOG_DIR / "bot.log"
        if log_path.exists():
            lines = log_path.read_text().split("\n")
            recent_errors = [l for l in lines[-200:] if "[ERROR]" in l or "[WARNING]" in l]
            diagnostics.append(f"RECENT ERRORS/WARNINGS (last 200 log lines):\n" +
                               "\n".join(recent_errors[-20:]))
    except Exception as e:
        diagnostics.append(f"Could not read logs: {e}")

    # 2. Active tasks
    active = task_manager.get_active_tasks()
    if active:
        task_info = [f"  - {t.task_id}: {t.text[:60]} ({t.phase}, {t.elapsed()})"
                     for t in active]
        diagnostics.append(f"ACTIVE TASKS ({len(active)}):\n" + "\n".join(task_info))
    else:
        diagnostics.append("ACTIVE TASKS: none")

    # 3. Client pool status
    from bot.agent import client_pool
    pool_status = client_pool.get_status()
    diagnostics.append(f"CLIENT POOL: {len(pool_status)} clients\n" +
                       "\n".join(f"  - {k}: queries={v['queries']}, age={v['age_min']}m"
                                 for k, v in pool_status.items()))

    # 4. Health check
    try:
        from integrations.whatsapp import get_new_messages_since
        await get_new_messages_since(datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))
        diagnostics.append("WA BRIDGE: OK (database readable)")
    except Exception as e:
        diagnostics.append(f"WA BRIDGE: FAILED — {e}")

    # 5. Memory/disk
    import subprocess
    try:
        result = subprocess.run(
            ["sh", "-c", "df -h /app/data | tail -1 && echo '---' && "
             "ls /proc/*/status 2>/dev/null | wc -l"],
            capture_output=True, text=True, timeout=5,
        )
        diagnostics.append(f"SYSTEM:\n{result.stdout.strip()}")
    except Exception:
        pass

    diag_text = "\n\n".join(diagnostics)

    prompt = (
        f"[SELF-DIAGNOSE] Analyze these diagnostics and report findings to "
        f"{'Telegram' if source == 'telegram' else 'WhatsApp'}.\n\n"
        f"{diag_text}\n\n"
        f"Instructions:\n"
        f"1. Analyze the errors and warnings — identify root causes\n"
        f"2. Check if any tasks are stuck or failing repeatedly\n"
        f"3. Report your findings briefly to the user\n"
        f"4. If you identify a code fix, suggest 'I can try to fix this via self-upgrade'\n"
        f"5. If auth is failing, suggest re-running credentials setup\n"
        f"6. Reply via {'telegram_send_message with chat_id=' + str(chat_id) if source == 'telegram' else 'whatsapp_send_message with recipient=' + chat_id}"
    )

    try:
        await process_system_task(prompt)
    except Exception as e:
        log.error(f"Self-diagnose failed: {e}")
        await _send_to_platform_simple(source, chat_id,
                                       f"⚠️ Self-diagnosis failed: {str(e)[:200]}")


async def _suggest_diagnose_on_failure(task: ActiveTask, error: str):
    """After a task failure, suggest running self-diagnose."""
    msg = (
        f"💡 Task failed. Send \"diagnose\" to run self-diagnostics, "
        f"or describe what you'd like me to fix."
    )
    await _send_to_platform_from_task(task, msg)


async def _send_to_platform_simple(source: str, chat_id: str, msg: str):
    """Send a message on a platform by source/chat_id."""
    try:
        if source == "whatsapp":
            from integrations.whatsapp import send_message
            await send_message(chat_id, msg)
        else:
            from integrations.telegram import send_message
            await send_message(msg, chat_id=int(chat_id))
    except Exception:
        pass


# === MESSAGE TRIAGE & DISPATCH ===

_CANCEL_RE = re.compile(
    r"(?:cancel|stop|abort|отмена|стоп|хватит|отменить|прекрати)",
    re.IGNORECASE,
)

_CANCEL_STRICT_RE = re.compile(
    r"^(?:cancel|stop|abort|отмена|стоп|хватит|отменить|прекрати)[\s!.]*$",
    re.IGNORECASE,
)

_DROP_CALL_RE = re.compile(
    r"^(?:drop|hang\s*up|end\s*call|drop\s*call|положи(?:\s*трубку)?|бросай|сбрось|повесь\s*трубку)[\s!.]*$",
    re.IGNORECASE,
)


def _is_cancel_message(text: str) -> bool:
    """Detect cancel intent — strict match or 'cancel this task and ...' patterns."""
    if _CANCEL_STRICT_RE.match(text):
        return True
    text_lower = text.lower().strip()
    # "cancel this task and ..." pattern
    if text_lower.startswith(("cancel this", "stop this", "cancel the task",
                              "stop the task", "отмени эту", "останови эту")):
        return True
    return False


async def _dispatch_task(parsed: dict):
    """Dispatch a message as a parallel task."""
    source = parsed.get("source", "telegram")
    chat_id = str(parsed.get("chat_id") or parsed.get("chat_jid") or
                  (TG_CHAT_ID if source == "telegram" else WA_FAMILY_GROUP_JID))
    user_name = parsed.get("user_name", "?")
    user_id = str(parsed.get("user_id", ""))
    text = parsed.get("text", "")

    # Register task
    task = task_manager.register_task(source, chat_id, user_name, user_id, text)
    parsed["model"] = MODEL_LONG

    handler = handle_whatsapp_message if source == "whatsapp" else handle_telegram_message

    async def _run():
        try:
            await handler(parsed, task)
            task_manager.complete_task(task.task_id)
        except Exception as e:
            task_manager.fail_task(task.task_id, str(e))

    task._asyncio_task = asyncio.create_task(_run())


async def message_processor():
    """Background task: reads from queue, dispatches parallel tasks."""
    while True:
        parsed = await _message_queue.get()
        try:
            text = parsed.get("text", "").strip()
            source = parsed.get("source", "telegram")
            chat_id = str(parsed.get("chat_id") or parsed.get("chat_jid") or
                          (TG_CHAT_ID if source == "telegram" else WA_FAMILY_GROUP_JID))

            # Handle reply-to-placeholder cancel (TG only)
            _handled = False
            reply_to = parsed.get("reply_to")
            if reply_to and reply_to.get("is_bot") and task_manager.active_count > 0:
                reply_msg_id = reply_to.get("message_id", 0)
                for t in task_manager.get_active_tasks():
                    if t.tg_placeholder_id == reply_msg_id and _CANCEL_RE.search(text):
                        log.info(f"Cancel by reply to placeholder: task {t.task_id}")
                        t.cancel_event.set()
                        if t._asyncio_task:
                            t._asyncio_task.cancel()
                        task_manager.fail_task(t.task_id, "Cancelled by user")
                        await _send_to_platform_simple(source, chat_id,
                            f"🛑 Cancelled: {t.text[:60]}")
                        _handled = True
                        break
            if _handled:
                continue

            # Handle cancel
            if _is_cancel_message(text) and task_manager.active_count > 0:
                target = task_manager.find_cancel_target(text)
                if target:
                    log.info(f"Cancelling task {target.task_id}: {target.text[:60]}")
                    target.cancel_event.set()
                    if target._asyncio_task:
                        target._asyncio_task.cancel()
                    task_manager.fail_task(target.task_id, "Cancelled by user")
                    await _send_to_platform_simple(source, chat_id,
                                                   f"🛑 Cancelled: {target.text[:60]}")
                    # If the cancel message has additional instructions, process them
                    # E.g., "cancel this task and check logs"
                    remaining = text.lower()
                    for word in ["cancel", "stop", "this task", "that task",
                                 "the task", "and ", "then "]:
                        remaining = remaining.replace(word, "").strip()
                    if remaining and len(remaining) > 5:
                        # Requeue the remaining instruction
                        parsed["text"] = remaining
                        await _message_queue.put(parsed)
                    continue

            # Handle drop call
            if _DROP_CALL_RE.match(text):
                from integrations.phone import get_active_calls, signal_drop_call
                if get_active_calls():
                    log.info(f"Drop call command from {parsed.get('user_name')}")
                    signal_drop_call()
                    continue

            # Handle self-diagnose command
            if _DIAGNOSE_RE.match(text):
                log.info(f"Self-diagnose requested by {parsed.get('user_name')}")
                asyncio.create_task(run_self_diagnose(source, chat_id))
                continue

            # Check parallel task capacity — tell user and skip (don't block)
            if not task_manager.can_accept_task():
                active = task_manager.get_active_tasks()
                tasks_desc = ", ".join(f'"{t.text[:40]}"' for t in active[:3])
                await _send_to_platform_simple(source, chat_id,
                    f"I'm working on {len(active)} things right now ({tasks_desc}). "
                    f"Send your message again once one finishes.")
                continue

            user = parsed.get("user_name", "?")
            log.info(f"Dispatching task for {user}: {text[:60]} "
                     f"(active={task_manager.active_count + 1}/{MAX_PARALLEL_TASKS})")

            await _dispatch_task(parsed)

        except Exception as e:
            log.error(f"Message processor error: {e}", exc_info=True)
            try:
                source = parsed.get("source", "telegram")
                chat_id = str(parsed.get("chat_id") or parsed.get("chat_jid") or TG_CHAT_ID)
                await _send_to_platform_simple(source, chat_id,
                    f"⚠️ Message processing failed: {str(e)[:200]}")
            except Exception:
                pass
        finally:
            _message_queue.task_done()


# === FASTAPI APP ===

@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup and shutdown."""
    log.info("=" * 60)
    log.info("Family Bot (Agent SDK) starting...")
    log.info(f"  Max parallel tasks: {MAX_PARALLEL_TASKS}")

    validate_startup()

    log.info("=" * 60)

    # --- Init: Database ---
    from bot.memory import init_db
    await init_db()

    # --- Init: Scheduled tasks (seed defaults if needed) ---
    from bot.scheduler import init_default_tasks
    init_default_tasks()

    # --- Init: Restore session map ---
    from bot.agent import client_pool
    await client_pool.restore_clients()

    # --- Init: RAG v4 chunk tables ---
    from bot.rag import init_rag_tables
    await init_rag_tables()

    # --- Init: Media cache cleanup ---
    from config import MEDIA_RETENTION_DAYS
    log.info(f"  [Media] Cache retention: {MEDIA_RETENTION_DAYS} days")
    await cleanup_expired_media()

    # --- Init: Telegram ---
    if TG_WEBHOOK_URL:
        from integrations.telegram import set_webhook
        await set_webhook(TG_WEBHOOK_URL)
        log.info(f"[TG] Webhook set: {TG_WEBHOOK_URL}")
    else:
        log.warning("[TG] No webhook URL — Telegram will not receive messages")

    # --- Init: WhatsApp ---
    log.info(f"[WA] Polling interval: {WA_POLL_INTERVAL}s, bridge: {WA_DB_PATH}")

    # --- Init: Phone (Vapi) ---
    from config import VAPI_API_KEY
    if VAPI_API_KEY:
        log.info("[Phone] Vapi configured — call events webhook on /call/events")
    else:
        log.info("[Phone] Vapi not configured")

    # Start background tasks
    scheduler_task = asyncio.create_task(scheduler_loop())
    processor_task = asyncio.create_task(message_processor())
    wa_poll_task = asyncio.create_task(wa_polling_loop())
    wa_batch_task = asyncio.create_task(_wa_batch_ticker())
    checkpoint_task = asyncio.create_task(_task_checkpoint_loop())

    # Send restart notification to both TG & WA
    restart_msg = f"Bot restarted ({GIT_COMMIT})"
    try:
        from integrations.telegram import send_message as tg_send
        await tg_send(restart_msg, chat_id=TG_CHAT_ID, parse_mode=None)
    except Exception as e:
        log.warning(f"Failed to send TG restart notification: {e}")
    try:
        from integrations.whatsapp import send_message as wa_send
        await wa_send(WA_FAMILY_GROUP_JID, restart_msg)
    except Exception as e:
        log.warning(f"Failed to send WA restart notification: {e}")

    # Run startup catchup
    asyncio.create_task(startup_catchup())

    yield

    # Shutdown
    log.info("Shutting down...")

    # Best-effort shutdown notification
    shutdown_msg = "Bot shutting down..."
    try:
        from integrations.telegram import send_message as tg_send
        await asyncio.wait_for(
            tg_send(shutdown_msg, chat_id=TG_CHAT_ID, parse_mode=None),
            timeout=3.0,
        )
    except Exception:
        pass
    try:
        from integrations.whatsapp import send_message as wa_send
        await asyncio.wait_for(
            wa_send(WA_FAMILY_GROUP_JID, shutdown_msg),
            timeout=3.0,
        )
    except Exception:
        pass

    for t in [scheduler_task, processor_task, wa_poll_task, wa_batch_task, checkpoint_task]:
        t.cancel()

    # Disconnect all SDK clients
    from bot.agent import client_pool
    await client_pool.disconnect_all()

    # Remove webhook
    if TG_WEBHOOK_URL:
        from integrations.telegram import delete_webhook
        await delete_webhook()


app = FastAPI(title="Family Bot", lifespan=lifespan)


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Receive Telegram webhook updates."""
    try:
        update = await request.json()
    except Exception:
        return Response(status_code=400)

    from integrations.telegram import parse_update

    parsed = parse_update(update)
    if parsed is None:
        return Response(status_code=200)

    if parsed.get("is_bot_message"):
        from bot.memory import store_message
        await store_message("telegram", parsed["user_name"], parsed["text"], "assistant")
        return Response(status_code=200)

    # SECURITY: Hard filter — only authorized users get processed
    tg_user_id = parsed.get("user_id")
    if tg_user_id and int(tg_user_id) not in TG_ALLOWED_USERS:
        log.warning(f"TG BLOCKED unauthorized user {tg_user_id} ({parsed.get('user_name', '?')}): "
                     f"{parsed.get('text', '')[:60]}")
        return Response(status_code=200)

    # Check if message is stale (sent before bot startup — TG delivers backlog on webhook reconnect)
    msg_date = update.get("message", {}).get("date", 0)
    if msg_date and msg_date < _BOT_STARTUP_TIME:
        log.info(f"TG stale message (pre-startup), storing for context: "
                 f"{parsed.get('user_name', '?')}: {parsed.get('text', '')[:60]}")
        try:
            from bot.memory import store_message
            await store_message(
                "telegram", parsed.get("user_name", "?"),
                parsed.get("text", ""), "user",
                str(parsed.get("user_id", "")), str(parsed.get("message_id", "")),
            )
        except Exception:
            pass
        return Response(status_code=200)

    parsed["source"] = "telegram"
    if parsed.get("reply_to"):
        rt = parsed["reply_to"]
        log.info(f"TG reply-to context: is_bot={rt.get('is_bot')}, "
                 f"text_len={len(rt.get('text', ''))}, "
                 f"text_preview={rt.get('text', '')[:80]!r}")
    await _message_queue.put(parsed)
    log.info(f"Queued TG message from {parsed['user_name']}: {parsed['text'][:80]}")

    return Response(status_code=200)


@app.get("/health")
async def health():
    """Health check endpoint."""
    from integrations.phone import get_active_calls
    from bot.agent import client_pool
    active_calls = get_active_calls()
    active_tasks = task_manager.get_active_tasks()
    return {
        "status": "ok",
        "version": "agent-sdk",
        "commit": GIT_COMMIT,
        "timestamp": datetime.now(TZ).isoformat(),
        "queue_size": _message_queue.qsize(),
        "active_tasks": len(active_tasks),
        "max_parallel": MAX_PARALLEL_TASKS,
        "tasks": [{"id": t.task_id, "text": t.text[:40], "phase": t.phase,
                    "elapsed": t.elapsed()} for t in active_tasks],
        "active_calls": len(active_calls),
        "wa_db_exists": WA_DB_PATH.exists(),
        "clients": client_pool.get_status(),
    }


@app.get("/")
async def root():
    return {"name": "Family Bot", "status": "running", "engine": "claude-agent-sdk"}


# === PHONE CALL WEBHOOK (Vapi) ===

@app.post("/call/events")
async def call_events(request: Request):
    """Vapi server webhook: receives all call events."""
    try:
        event = await request.json()
    except Exception:
        return Response(status_code=400)

    from integrations.phone import handle_call_event
    result = handle_call_event(event)

    if result:
        duration = result.get("duration_seconds", 0)
        cost = result.get("cost_usd", 0)
        mins = duration // 60
        secs = duration % 60

        summary_parts = [
            f"📞 <b>Call completed</b> ({mins}m {secs}s, ~${cost:.2f})",
            f"<b>To:</b> {result.get('to_number')}",
            f"<b>Objective:</b> {result.get('objective')}",
            f"<b>Ended:</b> {result.get('ended_reason', 'unknown')}",
        ]

        if result.get("summary"):
            summary_parts.append(f"\n<b>Summary:</b> {result['summary']}")

        if result.get("transcript"):
            transcript = result["transcript"]
            if len(transcript) > 2000:
                transcript = transcript[:2000] + "... [truncated]"
            summary_parts.append(f"\n<b>Transcript:</b>\n<pre>{transcript}</pre>")

        summary = "\n".join(summary_parts)

        source = result.get("source", "telegram")
        chat_id = result.get("chat_jid", WA_FAMILY_GROUP_JID) if source == "whatsapp" else str(TG_CHAT_ID)
        await _send_to_platform_simple(source, chat_id, summary)

    return Response(status_code=200)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        log_level="info",
    )
