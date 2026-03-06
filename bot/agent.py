"""Core AI agent — Claude Agent SDK wrapper with ClientPool for multi-chat sessions.

Leverages SDK's native session management, context compaction, tool execution loop,
streaming, and adaptive thinking.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, ResultMessage, SystemMessage,
)
from claude_agent_sdk.types import TextBlock, ThinkingBlock, ToolUseBlock

from config import (
    SESSION_TIMEOUT, MAX_TOOL_RESULT_CHARS, MAX_TURNS, DB_PATH,
    CLAUDE_CODE_OAUTH_TOKEN,
    MODEL_QUICK, MODEL_LONG,
)
from bot.hooks import (
    build_hooks, set_user_context, current_user_ctx,
    send_tool_called, intentional_silence, tool_status_callback, _SEND_TOOLS,
)
from bot.mcp_tools import get_custom_mcp_servers
from bot.mcp_config import get_external_mcp_servers
from bot.prompts import build_system_prompt

log = logging.getLogger(__name__)

# Garbage responses that Claude sometimes produces instead of actual replies
_GARBAGE_RESPONSES = {"no response requested", "no response requested.", "no response needed",
                      "no response needed.", "n/a", "none", "skip"}

# Built lazily on first use (depends on PARENT_NAMES from config)
_silence_patterns_compiled: list | None = None


def _get_silence_patterns() -> list:
    """Get compiled silence patterns (built once, cached)."""
    global _silence_patterns_compiled
    if _silence_patterns_compiled is None:
        from config import PARENT_NAMES
        _name_alt = "|".join(n.lower() for n in PARENT_NAMES) if PARENT_NAMES else "them"
        _silence_patterns_compiled = [
            rf"directed at (?:her|him|them|{_name_alt})",
            rf"tagging.*(?:{_name_alt}|not (?:at|for) the bot)",
            r"addressed to (?:her|him|them|each other)",
            r"not (?:at|for|directed at) the bot",
            rf"message is (?:for|to|between) (?:{_name_alt})",
            r"(?:don.t|shouldn.t|should not|won.t|will not) (?:respond|reply|intervene)",
            r"no (?:action|response|reply) (?:needed|required|necessary)",
        ]
    return _silence_patterns_compiled


@dataclass
class ManagedClient:
    """Wraps a ClaudeSDKClient with lifecycle metadata."""
    client: ClaudeSDKClient
    session_key: str
    session_id: str | None = None
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    query_count: int = 0


class ClientPool:
    """Manages one ClaudeSDKClient per chat session.

    Each client maintains a persistent Claude CLI subprocess with its own
    conversation context. Clients are kept alive for SESSION_TIMEOUT (24h)
    and disconnected on expiry.
    """

    def __init__(self):
        self._clients: dict[str, ManagedClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._session_map_loaded = False

    def _get_lock(self, session_key: str) -> asyncio.Lock:
        if session_key not in self._locks:
            self._locks[session_key] = asyncio.Lock()
        return self._locks[session_key]

    def _build_options(self, system_prompt: str, model: str = MODEL_LONG,
                       resume_session: str | None = None,
                       fork_session: bool = False) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions for a new or resumed client."""
        # Auth: prefer file-based credentials (~/.claude/.credentials.json) which
        # support auto-refresh via refresh token. Only fall back to env var token
        # if no credentials file exists (env var tokens can't auto-refresh).
        env = {}
        creds_file = Path.home() / ".claude" / ".credentials.json"
        if creds_file.exists():
            log.debug("Auth: using file-based credentials (auto-refresh enabled)")
        elif CLAUDE_CODE_OAUTH_TOKEN:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = CLAUDE_CODE_OAUTH_TOKEN
            log.debug("Auth: using env var token (no auto-refresh)")

        opts = ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=model,
            thinking={"type": "adaptive"},
            max_turns=MAX_TURNS,
            permission_mode="bypassPermissions",
            mcp_servers={**get_custom_mcp_servers(), **get_external_mcp_servers()},
            hooks=build_hooks(),
            env=env,
        )
        # Resume session if provided
        if resume_session:
            opts.resume = resume_session
        # Fork session if requested (task sessions fork from main to inherit context)
        if fork_session:
            opts.fork_session = True
        return opts

    async def get_main_session_id(self, source: str, chat_id: str) -> str | None:
        """Get the session_id of the main (non-task) session for a chat.

        Used to fork task sessions from the main session, inheriting context.
        """
        main_key = f"{source}:{chat_id}"
        # Check in-memory first
        if main_key in self._clients:
            return self._clients[main_key].session_id
        # Fall back to session map
        return await self._load_session_id(main_key)

    async def get_or_create(self, session_key: str,
                            system_prompt: str | None = None,
                            model: str = MODEL_LONG,
                            resume_session: str | None = None,
                            fork_session: bool = False) -> ManagedClient:
        """Get an existing client or create a new one for this session."""
        now = time.time()

        # Check if we have a live client
        if session_key in self._clients:
            mc = self._clients[session_key]
            if now - mc.last_activity < SESSION_TIMEOUT:
                mc.last_activity = now
                return mc
            else:
                # Expired — disconnect and create new
                log.info(f"Session expired: {session_key}")
                await self._disconnect_client(session_key)

        # Try to resume from session map if no resume_session given
        if not resume_session:
            resume_session = await self._load_session_id(session_key)

        # Build system prompt
        if system_prompt is None:
            system_prompt = build_system_prompt()

        # Create new client
        mode = "fork" if fork_session else ("resume" if resume_session else "new")
        log.info(f"Creating client for {session_key} (mode={mode})")
        opts = self._build_options(system_prompt, model, resume_session, fork_session)

        client = ClaudeSDKClient(options=opts)
        await client.connect()

        mc = ManagedClient(
            client=client,
            session_key=session_key,
            session_id=resume_session,
            created_at=now,
            last_activity=now,
        )
        self._clients[session_key] = mc
        return mc

    async def query(
        self,
        session_key: str,
        prompt: str,
        model: str = MODEL_LONG,
        source: str = "",
        user_id: str = "",
        user_name: str = "",
        on_tool_status: Callable | None = None,
        fork_from_session: str | None = None,
    ) -> AsyncIterator:
        """Send a query to the session's client and yield response messages.

        Acquires per-session lock to prevent concurrent processing.
        If fork_from_session is set, creates a forked session from the given session_id.
        """
        lock = self._get_lock(session_key)

        async with lock:
            # Set user context for hooks
            if source and user_id:
                set_user_context(source, user_id, user_name)
            if on_tool_status:
                tool_status_callback.set(on_tool_status)

            # Get or create client (fork if requested)
            if fork_from_session:
                mc = await self.get_or_create(
                    session_key, model=model,
                    resume_session=fork_from_session, fork_session=True,
                )
            else:
                mc = await self.get_or_create(session_key, model=model)
            mc.query_count += 1
            mc.last_activity = time.time()

            # Send query
            await mc.client.query(prompt)

            # Yield response messages
            async for message in mc.client.receive_response():
                # Capture session_id from SystemMessage (init) or ResultMessage
                if isinstance(message, SystemMessage):
                    sid = message.data.get("session_id")
                    if sid and not mc.session_id:
                        mc.session_id = sid
                        # Don't persist task-specific sessions (ephemeral)
                        if ":task-" not in session_key:
                            await self._save_session_id(session_key, mc.session_id)
                elif isinstance(message, ResultMessage) and message.session_id:
                    mc.session_id = message.session_id
                    if ":task-" not in session_key:
                        await self._save_session_id(session_key, mc.session_id)

                yield message

    async def cleanup_expired(self):
        """Disconnect clients that have been inactive longer than SESSION_TIMEOUT."""
        now = time.time()
        expired = [
            k for k, mc in self._clients.items()
            if now - mc.last_activity > SESSION_TIMEOUT
        ]
        for key in expired:
            log.info(f"Cleaning up expired session: {key} "
                     f"({self._clients[key].query_count} queries)")
            await self._disconnect_client(key)

    async def disconnect_all(self):
        """Disconnect all clients (called on shutdown)."""
        for key in list(self._clients.keys()):
            await self._disconnect_client(key)

    async def _disconnect_client(self, session_key: str):
        """Safely disconnect and remove a client, killing orphan subprocesses."""
        mc = self._clients.pop(session_key, None)
        if mc and mc.client:
            try:
                await mc.client.disconnect()
            except Exception as e:
                log.warning(f"Error disconnecting client {session_key}: {e}")
            # Kill any orphan child processes (Chromium, MCP servers) that SDK didn't clean up
            await self._kill_orphan_children()
        self._locks.pop(session_key, None)

    @staticmethod
    async def _kill_orphan_children():
        """Find and kill orphan Chromium/node processes left by MCP servers."""
        import subprocess
        try:
            result = subprocess.run(
                ["pgrep", "-f", "chromium|playwright"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                pids = result.stdout.strip().split("\n")
                for pid in pids:
                    try:
                        subprocess.run(["kill", "-9", pid], timeout=5)
                    except Exception:
                        pass
                log.info(f"Killed {len(pids)} orphan browser process(es)")
        except Exception:
            pass

    @staticmethod
    async def resource_watchdog():
        """Periodic check: kill orphan processes if resource usage is high.

        Call from scheduler loop every few minutes.
        """
        import subprocess
        try:
            # Count all child processes (main.py PID=1 in container)
            result = subprocess.run(
                ["sh", "-c", "ls /proc/*/status 2>/dev/null | wc -l"],
                capture_output=True, text=True, timeout=5,
            )
            proc_count = int(result.stdout.strip()) if result.stdout.strip() else 0

            # Check RSS memory of this process tree (in MB)
            mem_result = subprocess.run(
                ["sh", "-c", "cat /proc/1/status 2>/dev/null | grep VmRSS | awk '{print $2}'"],
                capture_output=True, text=True, timeout=5,
            )
            rss_kb = int(mem_result.stdout.strip()) if mem_result.stdout.strip() else 0
            rss_mb = rss_kb // 1024

            # Thresholds: >30 processes or >1.5GB RSS = orphan cleanup
            if proc_count > 30 or rss_mb > 1500:
                log.warning(f"Resource watchdog: {proc_count} procs, {rss_mb}MB RSS — killing orphans")
                await ClientPool._kill_orphan_children()
            elif proc_count > 10:
                log.debug(f"Resource watchdog: {proc_count} procs, {rss_mb}MB RSS — OK")
        except Exception as e:
            log.debug(f"Resource watchdog error: {e}")

    # --- Session ID persistence (SQLite) ---

    async def _save_session_id(self, session_key: str, session_id: str):
        """Save session_key → session_id mapping to SQLite."""
        import aiosqlite
        try:
            async with aiosqlite.connect(str(DB_PATH)) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO session_map (session_key, session_id, last_activity) "
                    "VALUES (?, ?, ?)",
                    (session_key, session_id, time.time()),
                )
                await db.commit()
        except Exception as e:
            log.warning(f"Failed to save session_id for {session_key}: {e}")

    async def _clear_session_id(self, session_key: str):
        """Remove session_id mapping from SQLite (used on fatal errors)."""
        import aiosqlite
        try:
            async with aiosqlite.connect(str(DB_PATH)) as db:
                await db.execute(
                    "DELETE FROM session_map WHERE session_key = ?",
                    (session_key,),
                )
                await db.commit()
            log.info(f"Cleared session_id for {session_key}")
        except Exception as e:
            log.warning(f"Failed to clear session_id for {session_key}: {e}")

    async def _load_session_id(self, session_key: str) -> str | None:
        """Load session_id for a session_key from SQLite."""
        import aiosqlite
        try:
            async with aiosqlite.connect(str(DB_PATH)) as db:
                cursor = await db.execute(
                    "SELECT session_id FROM session_map WHERE session_key = ? "
                    "AND last_activity > ?",
                    (session_key, time.time() - SESSION_TIMEOUT),
                )
                row = await cursor.fetchone()
                if row:
                    return row[0]
        except Exception as e:
            log.debug(f"Failed to load session_id for {session_key}: {e}")
        return None

    async def restore_clients(self):
        """Load session map from SQLite on startup. Clients are lazily created on first query."""
        import aiosqlite
        try:
            async with aiosqlite.connect(str(DB_PATH)) as db:
                # Ensure table exists
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS session_map ("
                    "  session_key TEXT PRIMARY KEY,"
                    "  session_id TEXT NOT NULL,"
                    "  last_activity REAL NOT NULL"
                    ")"
                )
                await db.commit()

                # Count active sessions
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM session_map WHERE last_activity > ?",
                    (time.time() - SESSION_TIMEOUT,),
                )
                count = (await cursor.fetchone())[0]
                log.info(f"Session map: {count} active session(s) available for resume")

        except Exception as e:
            log.warning(f"Failed to restore session map: {e}")

        self._session_map_loaded = True

    def get_status(self) -> dict:
        """Get pool status for health endpoint."""
        return {
            k: {
                "queries": mc.query_count,
                "session_id": mc.session_id[:8] + "..." if mc.session_id else None,
                "age_min": int((time.time() - mc.created_at) / 60),
            }
            for k, mc in self._clients.items()
        }


# Singleton pool instance
client_pool = ClientPool()


# ============================================================
# HIGH-LEVEL FUNCTIONS (called from main.py)
# ============================================================

async def process_incoming(
    source: str,
    user_name: str,
    user_id: str,
    text: str,
    message_id: str = "",
    media_info: dict | None = None,
    on_stream_chunk: Callable | None = None,
    on_thinking_start: Callable | None = None,
    on_tool_status: Callable | None = None,
    chat_id: str = "",
    model: str = MODEL_LONG,
    task_id: str = "",
    other_tasks_context: str = "",
) -> str:
    """Process an incoming message from any platform.

    Returns the final response text (may be empty if Claude sent via tool).
    """
    from bot.memory import store_message
    from config import TG_CHAT_ID, WA_FAMILY_GROUP_JID

    # Store incoming message
    try:
        await store_message(source, user_name, text, "user", user_id, message_id)
    except Exception as e:
        log.warning(f"Failed to store incoming message: {e}")

    # Build session key — use task_id for parallel tasks, shared session otherwise
    if task_id:
        session_key = f"{source}:{chat_id or 'default'}:task-{task_id}"
    else:
        session_key = f"{source}:{chat_id or 'default'}"

    # For task sessions: fork from main session + always inject DB context
    # (Main session only has system prompt; task conversations don't flow back)
    fork_from = None
    context_block = ""
    if task_id:
        try:
            fork_from = await client_pool.get_main_session_id(source, chat_id or "default")
            if fork_from:
                log.debug(f"Task {task_id}: will fork from main session {fork_from[:8]}...")
        except Exception as e:
            log.debug(f"Fork setup for task failed: {e}")
        # Always inject recent conversation from DB — task sessions are ephemeral
        # and prior task conversations don't persist in the main session
        try:
            from bot.memory import build_context_injection
            context_block = await build_context_injection(limit=20)
        except Exception as e:
            log.debug(f"Context injection failed: {e}")

    # Build the user prompt
    media_line = ""
    if media_info:
        mt = media_info.get("media_type", "file")
        path = media_info.get("local_path", "")
        media_line = f"\n[{mt.upper()} attached: {path}]"

    platform_tag = "[Telegram]" if source == "telegram" else "[WhatsApp]"
    if source == "telegram":
        tg_chat = chat_id or TG_CHAT_ID
        reply_instr = f'Reply via telegram_send_message with parse_mode="HTML" and chat_id={tg_chat}.'
    else:
        wa_recipient = chat_id or WA_FAMILY_GROUP_JID
        reply_instr = (
            f'Reply via whatsapp_send_message tool '
            f'with recipient="{wa_recipient}". '
            f'Do NOT use telegram_send_message — this is a WhatsApp message.'
        )

    other_ctx_line = f"\n\n{other_tasks_context}" if other_tasks_context else ""
    context_line = f"\n\n{context_block}" if context_block else ""
    user_prompt = (
        f"NEW MESSAGE {platform_tag} from {user_name} "
        f"(user ID: {user_id}, message ID: {message_id}):\n"
        f"{text}{media_line}\n\n"
        f"IMPORTANT: A 'Thinking...' status was already sent automatically. "
        f"Do NOT send any status/acknowledgment message. "
        f"Your FIRST send_message call must be the actual reply with real content.\n"
        f"You MUST reply to every message — never ignore or skip. "
        f"Even for short messages like 'test' or 'hi', respond conversationally.\n"
        f"Process and reply. {reply_instr}"
        f"{other_ctx_line}"
        f"{context_line}"
    )

    # Reset send tracking
    send_tool_called.set(False)
    intentional_silence.set(False)

    # Stream response
    final_text = ""
    thinking_notified = False
    tools_used = []
    query_start = time.time()

    try:
        async for message in client_pool.query(
            session_key=session_key,
            prompt=user_prompt,
            model=model,
            source=source,
            user_id=user_id,
            user_name=user_name,
            on_tool_status=on_tool_status,
            fork_from_session=fork_from,
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ThinkingBlock):
                        if not thinking_notified and on_thinking_start:
                            thinking_notified = True
                            try:
                                await on_thinking_start()
                            except Exception as e:
                                log.warning(f"Thinking start callback error: {e}")
                    elif isinstance(block, TextBlock):
                        final_text = block.text
                        if on_stream_chunk:
                            try:
                                await on_stream_chunk(final_text)
                            except Exception as e:
                                log.warning(f"Stream chunk callback error: {e}")
                    elif isinstance(block, ToolUseBlock):
                        # Track tool calls for status updates
                        bare_name = block.name.split("__")[-1]
                        tools_used.append(bare_name)
                        if bare_name in _SEND_TOOLS:
                            send_tool_called.set(True)
                        if on_tool_status:
                            try:
                                await on_tool_status([bare_name])
                            except Exception as e:
                                log.warning(f"Tool status callback error: {e}")
            elif isinstance(message, ResultMessage):
                elapsed = time.time() - query_start
                cost_str = f"${message.total_cost_usd:.4f}" if message.total_cost_usd else "?"
                model_label = "Sonnet" if "sonnet" in model else "Opus"
                log.info(
                    f"Query complete: {session_key} | {model_label} | {elapsed:.1f}s | "
                    f"turns={message.num_turns} cost={cost_str} | "
                    f"tools=[{', '.join(tools_used)}] | "
                    f"sent_via_tool={send_tool_called.get(False)} | "
                    f"result={len(message.result or '')} chars"
                )
                if message.result:
                    final_text = message.result
                if message.session_id and ":task-" not in session_key:
                    await client_pool._save_session_id(session_key, message.session_id)
    except Exception as e:
        # Fatal error during query — clear session to prevent pollution on retry
        log.error(f"Fatal error in query for {session_key}: {e}", exc_info=True)
        await client_pool._disconnect_client(session_key)
        if ":task-" not in session_key:
            await client_pool._clear_session_id(session_key)
        # Don't re-raise — let fallback send handle it or return empty
    finally:
        # Always disconnect task-specific (ephemeral) sessions to avoid leaking subprocesses
        if ":task-" in session_key:
            await client_pool._disconnect_client(session_key)

    # Fallback send: if Claude returned text but never called a send tool
    sent_reply = send_tool_called.get(False)
    # Filter non-reply garbage text (model sometimes outputs these instead of replying)
    text_lower = final_text.strip().lower() if final_text else ""
    if text_lower in _GARBAGE_RESPONSES:
        log.warning(f"Claude returned garbage response, suppressing: {final_text[:80]}")
        final_text = ""
    elif final_text and not sent_reply and any(re.search(p, text_lower) for p in _get_silence_patterns()):
        log.warning(f"Claude explained why not replying — suppressing: {final_text[:80]}")
        final_text = ""
        intentional_silence.set(True)
    if final_text and not sent_reply:
        log.warning(f"Claude didn't send reply via tool — fallback sending: {final_text[:80]}")
        try:
            if source == "telegram":
                from integrations.telegram import send_message as tg_send
                await tg_send(final_text[:4000], chat_id=int(chat_id) if chat_id else TG_CHAT_ID)
            else:
                from integrations.whatsapp import send_message as wa_send
                await wa_send(chat_id or WA_FAMILY_GROUP_JID, final_text[:4000])
        except Exception as e:
            log.error(f"Fallback send failed: {e}")

    # Store bot response + extract memory updates
    if final_text:
        try:
            await store_message(source, "Bot", final_text[:2000], "assistant")
        except Exception as e:
            log.warning(f"Failed to store bot response: {e}")
        try:
            await _persist_memory_updates(final_text)
        except Exception as e:
            log.warning(f"Failed to extract/persist memory updates: {e}")

    return final_text


async def process_system_task(prompt: str, model: str = MODEL_LONG) -> str:
    """Process a system-initiated task (proactive, catchup, email check).

    Uses an ephemeral query (no persistent session).
    """
    session_key = "system:ephemeral"

    final_text = ""
    async for message in client_pool.query(
        session_key=session_key,
        prompt=prompt,
        model=model,
    ):
        text_content = _extract_text(message)
        if text_content:
            final_text = text_content

    return final_text


# ============================================================
# HELPERS
# ============================================================

def _extract_text(message) -> str:
    """Extract text content from a SDK message object."""
    if hasattr(message, 'content') and isinstance(message.content, str):
        return message.content
    if hasattr(message, 'content') and isinstance(message.content, list):
        parts = []
        for block in message.content:
            if hasattr(block, 'text'):
                parts.append(block.text)
            elif isinstance(block, dict) and block.get('type') == 'text':
                parts.append(block.get('text', ''))
        return "\n".join(parts)
    if hasattr(message, 'text'):
        return message.text
    return ""


async def _persist_memory_updates(text: str):
    """Extract and persist KNOWLEDGE_UPDATE and FACTS_UPDATE from response text."""
    from bot.memory import store_knowledge_in_db, store_fact_in_db

    for match in re.findall(r'KNOWLEDGE_UPDATE:\s*"""(.*?)"""', text, re.DOTALL):
        content = match.strip()
        if content and len(content) > 10:
            try:
                await store_knowledge_in_db(content)
            except Exception as e:
                log.warning(f"Failed to persist knowledge to DB: {e}")

    for match in re.findall(r'FACTS_UPDATE:\s*(\{.*?\})', text, re.DOTALL):
        try:
            data = json.loads(match)
            for key, value in data.items():
                if key.startswith("_"):
                    continue
                try:
                    await store_fact_in_db(key, str(value))
                except Exception as e:
                    log.warning(f"Failed to persist fact '{key}' to DB: {e}")
        except json.JSONDecodeError:
            pass



