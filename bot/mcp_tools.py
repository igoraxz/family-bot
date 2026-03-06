"""Custom MCP tool servers for Claude Agent SDK.

Only tools that have NO native/external MCP equivalent are defined here.
External MCP servers handle the rest:
  - Playwright MCP: browser automation (navigate, screenshot, click, type, etc.)
  - WhatsApp MCP: messaging, contacts, chat history (send, list, search, etc.)
  - Google Workspace MCP: Gmail + Calendar (search, read, send, events, etc.)
  - SDK built-in: file ops (Read/Write/Edit/Glob/Grep), Bash, WebFetch, WebSearch

Custom tools defined here (26 tools in 3 servers):
  - family-messaging: Telegram (5 tools) + WhatsApp (5 tools)
  - family-services: Phone (2) + Image gen (2) + Deploy (2) + Scheduler (2)
  - family-memory: Memory search + context (5) + RAG search (3)
"""

import asyncio
import json
import logging
import time as _time
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server

from config import MAX_TOOL_RESULT_CHARS, TOOL_TIMEOUT

log = logging.getLogger(__name__)


# ============================================================
# HELPERS
# ============================================================

def _classify_error(e: Exception) -> str:
    """Classify an exception into a user-friendly category."""
    err_str = str(e).lower()
    err_type = type(e).__name__
    if "invalid_scope" in err_str or "invalid_grant" in err_str:
        return "AUTH_ERROR: Google OAuth credentials are invalid or expired. Tell the user that Gmail/Calendar access needs re-authorization."
    if "refresh" in err_str and ("token" in err_str or "credential" in err_str):
        return "AUTH_ERROR: Authentication credentials expired. Tell the user this service needs re-authorization."
    if "rate limit" in err_str or "429" in err_str or "too many requests" in err_str:
        return "RATE_LIMIT: Service is rate-limited. Wait a moment and the user can try again."
    if any(x in err_str for x in ["connection refused", "connect timeout", "name resolution", "unreachable"]):
        return f"SERVICE_DOWN: The service is unreachable ({err_type}). Tell the user and answer from what you already know."
    if "timeout" in err_str or "timed out" in err_str:
        return f"TIMEOUT: The service took too long to respond. Tell the user and try to answer from memory/context."
    if "permission" in err_str or "forbidden" in err_str or "403" in err_str:
        return f"PERMISSION_ERROR: Access denied ({err_str[:100]}). Tell the user."
    if "not found" in err_str or "404" in err_str:
        return f"NOT_FOUND: Resource not found ({err_str[:100]})."
    return f"ERROR: {err_str[:200]}"


def _text_response(data: Any) -> dict:
    """Format any data as an MCP text content response."""
    if isinstance(data, dict):
        text = json.dumps(data, ensure_ascii=False, default=str)
    else:
        text = str(data)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "\n... [truncated]"
    return {"content": [{"type": "text", "text": text}]}


async def _run_with_timeout(coro, tool_name: str, timeout: int = 0) -> dict:
    """Execute a coroutine with timeout and error classification. Returns MCP response."""
    effective_timeout = timeout or TOOL_TIMEOUT
    start = _time.time()
    try:
        result = await asyncio.wait_for(coro, timeout=effective_timeout)
        elapsed = _time.time() - start
        log.debug(f"Tool {tool_name} returned ({elapsed:.1f}s)")
        return _text_response(result)
    except asyncio.TimeoutError:
        log.error(f"Tool {tool_name} timed out after {TOOL_TIMEOUT}s")
        return _text_response({"error": f"TIMEOUT: {tool_name} did not respond within {TOOL_TIMEOUT}s."})
    except PermissionError as e:
        return _text_response({"error": str(e)})
    except Exception as e:
        elapsed = _time.time() - start
        log.error(f"Tool {tool_name} failed after {elapsed:.1f}s: {e}", exc_info=True)
        return _text_response({"error": _classify_error(e)})


# ============================================================
# TELEGRAM TOOLS (no MCP alternative has edit_message)
# ============================================================

@tool("telegram_send_message", "Send a message to a Telegram chat", {
    "text": str, "parse_mode": str, "chat_id": int,
})
async def telegram_send_message(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import send_message
        from config import TG_CHAT_ID
        result = await send_message(
            args["text"],
            chat_id=args.get("chat_id") or TG_CHAT_ID,
            parse_mode=args.get("parse_mode", "HTML"),
        )
        if result:
            # Store bot response for context continuity across tasks
            try:
                from bot.memory import store_message as store_msg
                await store_msg("telegram", "Bot", args["text"][:2000], "assistant")
            except Exception:
                pass
            return {"success": True, "message_id": result.get("message_id")}
        return {"success": False, "error": "Failed to send message"}
    return await _run_with_timeout(_do(), "telegram_send_message")


@tool("telegram_edit_message", "Edit an existing Telegram message", {
    "message_id": int, "text": str, "chat_id": int,
})
async def telegram_edit_message(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import edit_message
        from config import TG_CHAT_ID
        result = await edit_message(
            args["message_id"], args["text"],
            chat_id=args.get("chat_id") or TG_CHAT_ID,
        )
        return {"success": result is not None}
    return await _run_with_timeout(_do(), "telegram_edit_message")


@tool("telegram_delete_message", "Delete a Telegram message", {
    "message_id": int, "chat_id": int,
})
async def telegram_delete_message(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import delete_message
        from config import TG_CHAT_ID
        result = await delete_message(args["message_id"], chat_id=args.get("chat_id") or TG_CHAT_ID)
        return {"success": result}
    return await _run_with_timeout(_do(), "telegram_delete_message")


@tool("telegram_send_photo", "Send a photo to a Telegram chat", {
    "photo_path": str, "caption": str, "chat_id": int,
})
async def telegram_send_photo(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import send_photo
        from config import TG_CHAT_ID
        result = await send_photo(
            args["photo_path"],
            args.get("caption", ""),
            chat_id=args.get("chat_id") or TG_CHAT_ID,
        )
        if result:
            return {"success": True, "message_id": result.get("message_id")}
        return {"success": False, "error": "Failed to send photo"}
    return await _run_with_timeout(_do(), "telegram_send_photo")


@tool("telegram_send_document", "Send a document to a Telegram chat", {
    "document_path": str, "caption": str, "chat_id": int,
})
async def telegram_send_document(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.telegram import send_document
        from config import TG_CHAT_ID
        result = await send_document(
            args["document_path"],
            args.get("caption", ""),
            chat_id=args.get("chat_id") or TG_CHAT_ID,
        )
        if result:
            return {"success": True, "message_id": result.get("message_id")}
        return {"success": False, "error": "Failed to send document"}
    return await _run_with_timeout(_do(), "telegram_send_document")



# ============================================================
# WHATSAPP TOOLS (custom — always loaded, bypasses ToolSearch)
# ============================================================
# The external WhatsApp MCP server tools are deferred-loaded via SDK ToolSearch,
# which means the model often can't find them and falls back to Telegram.
# This custom tool wraps integrations.whatsapp.send_message directly.

@tool("whatsapp_send_message", "Send a WhatsApp message to a chat or group. Use this for ALL WhatsApp replies.", {
    "recipient": str, "message": str,
})
async def whatsapp_send_message(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.whatsapp import send_message
        result = await send_message(args["recipient"], args["message"])
        # Store bot response for context continuity across tasks
        try:
            from bot.memory import store_message as store_msg
            await store_msg("whatsapp", "Bot", args["message"][:2000], "assistant")
        except Exception:
            pass
        return result
    return await _run_with_timeout(_do(), "whatsapp_send_message")


@tool("whatsapp_send_file", "Send a file (image, document, video) via WhatsApp. Auto-detects type by extension.", {
    "recipient": str, "file_path": str,
})
async def whatsapp_send_file(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.whatsapp import send_file
        result = await send_file(args["recipient"], args["file_path"])
        return result
    return await _run_with_timeout(_do(), "whatsapp_send_file")


@tool("whatsapp_list_messages", "Read recent WhatsApp messages from a chat. Returns messages in chronological order.", {
    "chat_jid": str, "limit": int, "query": str, "sender_phone": str, "after": str,
})
async def whatsapp_list_messages(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.whatsapp import list_messages
        msgs = await list_messages(
            chat_jid=args.get("chat_jid"),
            after=args.get("after"),
            sender_phone=args.get("sender_phone"),
            query=args.get("query"),
            limit=args.get("limit", 20),
        )
        return {"messages": msgs, "count": len(msgs)}
    return await _run_with_timeout(_do(), "whatsapp_list_messages")


@tool("whatsapp_list_chats", "List WhatsApp chats with their last message. Use to find chat JIDs.", {
    "query": str, "limit": int,
})
async def whatsapp_list_chats(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.whatsapp import list_chats
        chats = await list_chats(
            query=args.get("query"),
            limit=args.get("limit", 20),
        )
        return {"chats": chats, "count": len(chats)}
    return await _run_with_timeout(_do(), "whatsapp_list_chats")


@tool("whatsapp_search_contacts", "Search WhatsApp contacts by name or phone number.", {
    "query": str,
})
async def whatsapp_search_contacts(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.whatsapp import search_contacts
        contacts = await search_contacts(args["query"])
        return {"contacts": contacts, "count": len(contacts)}
    return await _run_with_timeout(_do(), "whatsapp_search_contacts")


# ============================================================
# IMAGE GENERATION (Gemini Imagen 3)
# ============================================================

@tool("generate_image", "Generate a NEW image from a text prompt using Imagen 4.0. Returns a file path. Cannot include real people.", {
    "prompt": str, "filename": str,
})
async def generate_image(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.gemini import generate_image as _gen
        result = await _gen(
            args["prompt"],
            filename=args.get("filename", "generated.png"),
        )
        if "error" in result:
            return result
        return {"success": True, "file_path": result["file_path"]}
    return await _run_with_timeout(_do(), "generate_image")


@tool("edit_image", "Edit an EXISTING image using Gemini. Send an image + text instructions describing the edit. Use for: style changes, adding/removing objects, background swap, color correction, etc.", {
    "image_path": str, "prompt": str, "filename": str, "use_pro": bool,
})
async def edit_image(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.gemini import edit_image as _edit
        result = await _edit(
            image_path=args["image_path"],
            prompt=args["prompt"],
            filename=args.get("filename", "edited.png"),
            use_pro=args.get("use_pro", False),
        )
        if "error" in result:
            return result
        return {"success": True, "file_path": result["file_path"]}
    return await _run_with_timeout(_do(), "edit_image")


# ============================================================
# PHONE CALL TOOLS (Vapi-specific)
# ============================================================

@tool("phone_call", "Make an outbound phone call via Vapi AI voice agent. ALWAYS show a call plan and get approval first!", {
    "to_number": str, "objective": str, "first_message": str,
    "authorized_info": str, "voice": str, "language": str,
})
async def phone_call(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.phone import make_call
        return await make_call(
            args["to_number"], args["objective"], args["first_message"],
            authorized_info=args.get("authorized_info", ""),
            voice=args.get("voice", "male"),
            language=args.get("language", ""),
        )
    return await _run_with_timeout(_do(), "phone_call")


@tool("phone_get_transcript", "Get the transcript and outcome of a completed phone call", {
    "call_id": str,
})
async def phone_get_transcript(args: dict[str, Any]) -> dict:
    async def _do():
        from integrations.phone import get_call_transcript
        return await get_call_transcript(args["call_id"])
    return await _run_with_timeout(_do(), "phone_get_transcript")


# ============================================================
# MEMORY TOOLS (custom SQLite FTS5)
# ============================================================

@tool("get_recent_conversation", "Load recent conversation messages from both Telegram and WhatsApp", {
    "limit": int,
})
async def get_recent_conversation(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.memory import get_recent_messages, format_recent_context
        limit = min(args.get("limit", 15), 50)
        messages = await get_recent_messages(limit=limit)
        if not messages:
            return "No recent conversation history."
        return format_recent_context(messages)
    return await _run_with_timeout(_do(), "get_recent_conversation")


@tool("search_memory", "Search ALL bot memory (messages, facts, knowledge, summaries, cached media) by keyword. Returns full message text — no truncation. Use get_message_context to expand around a hit.", {
    "query": str, "limit": int,
})
async def search_memory(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.memory import search_all_memory
        results = await search_all_memory(args["query"], limit=args.get("limit", 20))
        output_parts = []
        if results.get("messages"):
            output_parts.append("MESSAGES (full text, sorted by relevance):")
            for m in results["messages"]:
                mid = m.get("id", "?")
                ts = m.get("timestamp", "")[:16]
                src = m.get("source", "")
                user = m.get("user_name", "")
                text = m.get("text", "")  # full text, no truncation
                output_parts.append(f"  [msg#{mid} {ts} {src}] {user}: {text}")
        if results.get("facts"):
            output_parts.append("\nFACTS:")
            for f in results["facts"]:
                output_parts.append(f"  [{f.get('category', '')}] {f.get('fact_key', '')}: {f.get('fact_value', '')}")
        if results.get("knowledge"):
            output_parts.append("\nKNOWLEDGE:")
            for k in results["knowledge"]:
                output_parts.append(f"  [{k.get('created_at', '')[:10]}] {k.get('content', '')}")
        if results.get("summaries"):
            output_parts.append("\nSUMMARIES:")
            for s in results["summaries"]:
                output_parts.append(f"  [{s.get('created_at', '')[:10]}] {s.get('summary', '')}")
        if results.get("media"):
            output_parts.append("\nMEDIA:")
            for mc in results["media"]:
                output_parts.append(f"  [{mc.get('cached_at', '')[:10]}] {mc.get('media_type', '')}: {mc.get('original_filename', '')} from {mc.get('sender_name', '')} — {mc.get('description', '')}")
        if not output_parts:
            return "No results found for this query."
        return "\n".join(output_parts)
    return await _run_with_timeout(_do(), "search_memory")


@tool("get_message_context", "Get full conversation context around a specific message ID. Use after search_memory to see surrounding messages.", {
    "message_id": int, "before": int, "after": int,
})
async def get_message_context(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.memory import get_messages_around
        messages = await get_messages_around(
            args["message_id"],
            before=args.get("before", 10),
            after=args.get("after", 10),
        )
        if not messages:
            return "No messages found around this ID."
        lines = []
        for m in messages:
            mid = m.get("id", "?")
            ts = m.get("timestamp", "")[:16]
            src = m.get("source", "")
            user = m.get("user_name", "")
            role = m.get("role", "")
            text = m.get("text", "")
            marker = " <<<" if mid == args["message_id"] else ""
            lines.append(f"[msg#{mid} {ts} {src} {role}] {user}: {text}{marker}")
        return "\n".join(lines)
    return await _run_with_timeout(_do(), "get_message_context")


@tool("search_media", "Search cached media files (photos, documents, voice messages, etc.) by keyword", {
    "query": str, "media_type": str, "limit": int,
})
async def search_media(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.memory import search_media_cache, list_cached_media
        query = args.get("query", "")
        if query:
            results = await search_media_cache(query, limit=args.get("limit", 10))
        else:
            results = await list_cached_media(media_type=args.get("media_type", ""), limit=args.get("limit", 10))
        if not results:
            return "No cached media files found."
        lines = ["Cached media files:"]
        for r in results:
            size_kb = r.get("file_size", 0) / 1024
            lines.append(
                f"  [{r.get('cached_at', '')[:10]}] {r.get('media_type', '')} | "
                f"{r.get('original_filename', r.get('filename', ''))} | "
                f"{size_kb:.0f}KB | from {r.get('sender_name', '?')} | "
                f"path: {r.get('file_path', '')}"
            )
            if r.get("description"):
                lines.append(f"    {r['description'][:200]}")
        return "\n".join(lines)
    return await _run_with_timeout(_do(), "search_media")


@tool("media_cache_stats", "Get statistics about the media cache", {})
async def media_cache_stats(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.memory import get_media_cache_stats
        return await get_media_cache_stats()
    return await _run_with_timeout(_do(), "media_cache_stats")


# ============================================================
# RAG v4 — CHUNK-BASED SEMANTIC SEARCH
# ============================================================

@tool("rag_search", "Semantic search over past conversations by MEANING. Returns 7-message chunk windows with similarity scores and message ID ranges for drill-down. Use for questions like 'what did we discuss about school?' or 'when did we talk about travel plans?'. After getting results, use get_message_context to expand around a specific msg ID range.", {
    "query": str, "top_k": int,
})
async def rag_search_tool(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.rag import rag_search
        query = args.get("query", "")
        if not query:
            return "Please provide a search query."
        top_k = min(args.get("top_k", 5), 15)
        results = await rag_search(query, top_k=top_k)

        if not results:
            return "No relevant chunks found. Try different phrasing, or use search_memory for keyword search."

        parts = [f"FOUND {len(results)} RELEVANT CONVERSATION CHUNKS:"]
        for i, r in enumerate(results, 1):
            parts.append(f"\n--- Chunk {i} (similarity={r['similarity']}, msgs {r['start_msg_id']}-{r['end_msg_id']}, {r['start_ts'][:10]}) ---")
            parts.append(r["chunk_text"])
            parts.append(f"  [Senders: {r['senders']} | Source: {r['source']}]")
            parts.append(f"  [Drill-down: get_message_context msg_id={r['start_msg_id']} to expand]")
        return "\n".join(parts)
    return await _run_with_timeout(_do(), "rag_search", timeout=30)


@tool("rag_backfill", "Rebuild ALL RAG chunks from scratch. Reads all conversation messages, creates 7-message sliding window chunks, embeds them. Run this to (re)build the semantic search index.", {})
async def rag_backfill_tool(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.rag import backfill_chunks, get_rag_stats

        result = await backfill_chunks()

        if "error" in result:
            return f"Backfill error: {result['error']}"

        stats = await get_rag_stats()
        return (
            f"RAG backfill complete!\n"
            f"Messages processed: {result['total_messages']}\n"
            f"Chunks created: {result['chunks_created']}\n"
            f"Chunks failed: {result.get('chunks_failed', 0)}\n"
            f"Time: {result['time_sec']}s\n"
            f"Index: {stats['total_chunks']} chunks covering msgs {stats['msg_range']}"
        )
    return await _run_with_timeout(_do(), "rag_backfill", timeout=300)


@tool("rag_stats", "Get RAG chunk index statistics — number of chunks, message coverage range.", {})
async def rag_stats_tool(args: dict[str, Any]) -> dict:
    async def _do():
        from bot.rag import get_rag_stats
        stats = await get_rag_stats()
        return (
            f"RAG Stats:\n"
            f"Total messages: {stats['total_messages']}\n"
            f"Total chunks: {stats['total_chunks']}\n"
            f"Message range: {stats['msg_range']}"
        )
    return await _run_with_timeout(_do(), "rag_stats")


# ============================================================
# MCP SERVER FACTORIES
# ============================================================

def create_messaging_server():
    """Create the family-messaging MCP server (Telegram + WhatsApp send)."""
    return create_sdk_mcp_server(
        name="family-messaging",
        version="1.0.0",
        tools=[
            telegram_send_message, telegram_edit_message, telegram_delete_message,
            telegram_send_photo, telegram_send_document,
            whatsapp_send_message, whatsapp_send_file, whatsapp_list_messages,
            whatsapp_list_chats, whatsapp_search_contacts,
        ],
    )



@tool("deploy_bot", "Trigger a Docker rebuild and restart of the bot. Writes a trigger file that the host watcher picks up. Actions: rebuild (default), restart (no rebuild), rebuild-all (all services).", {
    "action": {"type": "string", "description": "Action: rebuild, restart, or rebuild-all"},
    "reason": {"type": "string", "description": "Why this deploy is needed (shown in logs)"},
})
async def deploy_bot(args: dict[str, Any]) -> dict:
    """Trigger a Docker deploy via host watcher."""
    import json
    from pathlib import Path
    from datetime import datetime

    action = args.get("action", "rebuild")
    reason = args.get("reason", "bot self-upgrade")

    if action not in ("rebuild", "restart", "rebuild-all"):
        return {"error": f"Unknown action: {action}. Use: rebuild, restart, rebuild-all"}

    # Use /host-repo/deploy/ — accessible by both bot container and host watcher
    deploy_dir = Path("/host-repo/deploy")
    trigger_path = deploy_dir / "deploy_trigger.json"
    result_path = deploy_dir / "deploy_result.json"

    # Check if a deploy is already in progress
    if trigger_path.exists():
        return {"error": "Deploy already triggered — waiting for host watcher to pick it up"}
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text())
            if result.get("status") == "in_progress":
                return {"status": "in_progress", "message": "Deploy is running on host..."}
        except Exception:
            pass

    # Write trigger
    trigger = {
        "action": action,
        "reason": reason,
        "timestamp": datetime.now().isoformat(),
    }
    trigger_path.write_text(json.dumps(trigger, indent=2))

    return {
        "status": "triggered",
        "action": action,
        "reason": reason,
        "message": f"Deploy trigger written. Host watcher will {action} bot-core. Bot will restart in ~30-120s.",
    }


@tool("deploy_status", "Check the status of the last deploy triggered by deploy_bot.", {})
async def deploy_status(args: dict[str, Any]) -> dict:
    """Check deploy status from the result file."""
    import json
    from pathlib import Path

    deploy_dir = Path("/host-repo/deploy")
    result_path = deploy_dir / "deploy_result.json"
    trigger_path = deploy_dir / "deploy_trigger.json"

    if trigger_path.exists():
        return {"status": "pending", "message": "Trigger written, waiting for host watcher..."}
    if not result_path.exists():
        return {"status": "no_deploy", "message": "No recent deploy found"}

    try:
        result = json.loads(result_path.read_text())
        return result
    except Exception as e:
        return {"error": f"Cannot read result: {e}"}


@tool("list_scheduled_tasks", "List all scheduled tasks with their status, times, and IDs.", {})
async def list_scheduled_tasks(args: dict[str, Any]) -> dict:
    """List all scheduled tasks."""
    from bot.scheduler import list_tasks, format_task_list
    tasks = list_tasks()
    return {"tasks": tasks, "formatted": format_task_list(tasks)}


@tool("manage_scheduled_task", "Add, edit, delete, or toggle a scheduled task. Use action='add' to create, 'edit' to modify, 'delete' to remove, 'toggle' to enable/disable.", {
    "action": {"type": "string", "description": "Action: add, edit, delete, toggle"},
    "task_id": {"type": "string", "description": "Task ID (required for edit/delete/toggle)"},
    "name": {"type": "string", "description": "Task name (for add/edit)"},
    "hour": {"type": "integer", "description": "Hour 0-23 (for add/edit)"},
    "minute": {"type": "integer", "description": "Minute 0-59 (for add/edit)"},
    "days": {"type": "array", "items": {"type": "string"}, "description": "Days: ['daily'], ['weekdays'], ['weekends'], or specific days like ['mon','wed','fri']"},
    "prompt": {"type": "string", "description": "The prompt/instruction to execute when the task fires. Should include what to check and how to format the message."},
    "platform": {"type": "string", "description": "Where to send output: telegram, whatsapp, or both (default: telegram)"},
    "enabled": {"type": "boolean", "description": "Whether the task is enabled (default: true)"},
})
async def manage_scheduled_task(args: dict[str, Any]) -> dict:
    """Manage scheduled tasks (CRUD + toggle)."""
    from bot.scheduler import add_task, update_task, delete_task, toggle_task, get_task

    action = args.get("action", "").lower()

    if action == "add":
        name = args.get("name")
        hour = args.get("hour")
        minute = args.get("minute", 0)
        prompt = args.get("prompt")
        if not name or hour is None or not prompt:
            return {"error": "Required fields for add: name, hour, prompt"}
        task = add_task(
            name=name, hour=hour, minute=minute, prompt=prompt,
            days=args.get("days"), platform=args.get("platform", "telegram"),
            enabled=args.get("enabled", True),
        )
        return {"status": "created", "task": task}

    elif action == "edit":
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required for edit"}
        updates = {}
        for key in ("name", "hour", "minute", "days", "prompt", "platform", "enabled"):
            if key in args and args[key] is not None:
                updates[key] = args[key]
        task = update_task(task_id, **updates)
        if task:
            return {"status": "updated", "task": task}
        return {"error": f"Task {task_id} not found"}

    elif action == "delete":
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required for delete"}
        if delete_task(task_id):
            return {"status": "deleted", "task_id": task_id}
        return {"error": f"Task {task_id} not found"}

    elif action == "toggle":
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required for toggle"}
        task = toggle_task(task_id)
        if task:
            return {"status": "toggled", "task": task}
        return {"error": f"Task {task_id} not found"}

    else:
        return {"error": f"Unknown action: {action}. Use: add, edit, delete, toggle"}


def create_services_server():
    """Create the family-services MCP server (Phone + Image gen + Deploy + Scheduler)."""
    return create_sdk_mcp_server(
        name="family-services",
        version="1.0.0",
        tools=[
            phone_call, phone_get_transcript,
            generate_image, edit_image,
            deploy_bot, deploy_status,
            list_scheduled_tasks, manage_scheduled_task,
        ],
    )


def create_memory_server():
    """Create the family-memory MCP server."""
    return create_sdk_mcp_server(
        name="family-memory",
        version="1.0.0",
        tools=[
            get_recent_conversation, search_memory, search_media, media_cache_stats,
            rag_search_tool, rag_backfill_tool, rag_stats_tool,
        ],
    )


def get_custom_mcp_servers() -> dict:
    """Create and return all custom in-process MCP servers."""
    return {
        "family-messaging": create_messaging_server(),
        "family-services": create_services_server(),
        "family-memory": create_memory_server(),
    }
