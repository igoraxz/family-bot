"""WhatsApp integration — Go bridge HTTP API for sending + direct SQLite for reading."""

import logging
from typing import Optional

import aiosqlite
import httpx

from config import WA_API_URL, WA_DB_PATH, WA_ALLOWED_PHONES

log = logging.getLogger(__name__)

_http: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=30.0)
    return _http


# ============================================================
# SENDING (via Go bridge HTTP API)
# ============================================================

async def send_message(recipient: str, message: str) -> dict:
    """Send a WhatsApp text message."""
    client = await get_client()
    try:
        resp = await client.post(f"{WA_API_URL}/send", json={
            "recipient": recipient,
            "message": message,
        })
        data = resp.json()
        return {"success": data.get("success", False), "message": data.get("message", "")}
    except Exception as e:
        log.error(f"WA send_message error: {e}")
        return {"success": False, "message": str(e)}


async def send_file(recipient: str, media_path: str, caption: str = "") -> dict:
    """Send a file via WhatsApp.

    Reads the file locally and sends it as base64 to the bridge API,
    avoiding cross-container filesystem issues.
    """
    import base64
    from pathlib import Path

    path = Path(media_path)
    if not path.exists():
        return {"success": False, "message": f"File not found: {media_path}"}

    try:
        media_data = base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception as e:
        return {"success": False, "message": f"Failed to read file: {e}"}

    client = await get_client()
    try:
        resp = await client.post(f"{WA_API_URL}/send", json={
            "recipient": recipient,
            "message": caption,
            "media_data": media_data,
            "filename": path.name,
        }, timeout=60.0)
        data = resp.json()
        return {"success": data.get("success", False), "message": data.get("message", "")}
    except Exception as e:
        log.error(f"WA send_file error: {e}")
        return {"success": False, "message": str(e)}


async def send_location(recipient: str, latitude: float, longitude: float,
                        name: str = "", address: str = "") -> dict:
    """Send a location pin via WhatsApp."""
    client = await get_client()
    try:
        resp = await client.post(f"{WA_API_URL}/send_location", json={
            "recipient": recipient,
            "latitude": latitude,
            "longitude": longitude,
            "name": name,
            "address": address,
        })
        data = resp.json()
        return {"success": data.get("success", False), "message": data.get("message", "")}
    except Exception as e:
        log.error(f"WA send_location error: {e}")
        return {"success": False, "message": str(e)}


async def download_media(message_id: str, chat_jid: str) -> dict:
    """Download media from a WA message via the bridge."""
    client = await get_client()
    try:
        resp = await client.post(f"{WA_API_URL}/download", json={
            "message_id": message_id,
            "chat_jid": chat_jid,
        })
        data = resp.json()
        if data.get("success"):
            path = data.get("path", "")
            # Bridge returns paths relative to its own container (/app/store/...),
            # but bot-core mounts the same volume at /app/wa-data/
            if path.startswith("/app/store/"):
                path = "/app/wa-data/" + path[len("/app/store/"):]
            return {"success": True, "path": path, "filename": data.get("filename", "")}
        return {"success": False, "message": data.get("message", "Download failed")}
    except Exception as e:
        log.error(f"WA download_media error: {e}")
        return {"success": False, "message": str(e)}


# ============================================================
# READING (via direct SQLite DB access — bridge's messages.db)
# ============================================================

def _db_path() -> str:
    return str(WA_DB_PATH)


def _wa_store_path() -> str:
    """Path to the bridge's whatsapp.db (same volume, sibling of messages.db)."""
    return str(WA_DB_PATH.parent / "whatsapp.db")


async def _resolve_lid_to_phone(lid: str) -> Optional[str]:
    """Resolve a WhatsApp LID to a phone number via the bridge's lid_map table."""
    try:
        async with aiosqlite.connect(_wa_store_path()) as db:
            cursor = await db.execute(
                "SELECT pn FROM whatsmeow_lid_map WHERE lid = ? LIMIT 1", (lid,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None
    except Exception:
        return None


async def _get_sender_name(db: aiosqlite.Connection, sender_jid: str) -> str:
    """Resolve a sender JID to a display name."""
    cursor = await db.execute(
        "SELECT name FROM chats WHERE jid = ? LIMIT 1", (sender_jid,)
    )
    row = await cursor.fetchone()
    if row and row[0]:
        return row[0]
    if '@' in sender_jid:
        phone_part = sender_jid.split('@')[0]
    else:
        phone_part = sender_jid
    cursor = await db.execute(
        "SELECT name FROM chats WHERE jid LIKE ? LIMIT 1", (f"%{phone_part}%",)
    )
    row = await cursor.fetchone()
    return row[0] if row and row[0] else sender_jid


async def list_messages(
    chat_jid: Optional[str] = None,
    after: Optional[str] = None,
    sender_phone: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """Query messages from the WA bridge SQLite DB."""
    try:
        async with aiosqlite.connect(_db_path()) as db:
            parts = [
                "SELECT m.timestamp, m.sender, c.name, m.content, m.is_from_me, "
                "c.jid, m.id, m.media_type "
                "FROM messages m JOIN chats c ON m.chat_jid = c.jid"
            ]
            where = []
            params = []

            if chat_jid:
                where.append("m.chat_jid = ?")
                params.append(chat_jid)
            if after:
                where.append("m.timestamp > ?")
                params.append(after)
            if sender_phone:
                where.append("m.sender = ?")
                params.append(sender_phone)
            if query:
                where.append("LOWER(m.content) LIKE LOWER(?)")
                params.append(f"%{query}%")

            if where:
                parts.append("WHERE " + " AND ".join(where))
            parts.append("ORDER BY m.timestamp DESC LIMIT ?")
            params.append(limit)

            cursor = await db.execute(" ".join(parts), tuple(params))
            rows = await cursor.fetchall()

            messages = []
            for row in rows:
                sender_name = await _get_sender_name(db, row[1]) if not row[4] else "Me"
                messages.append({
                    "timestamp": row[0],
                    "sender": row[1],
                    "sender_name": sender_name,
                    "chat_name": row[2],
                    "content": row[3],
                    "is_from_me": bool(row[4]),
                    "chat_jid": row[5],
                    "id": row[6],
                    "media_type": row[7],
                })
            return list(reversed(messages))  # chronological order
    except Exception as e:
        log.error(f"WA list_messages error: {e}")
        return []


async def list_chats(query: Optional[str] = None, limit: int = 20) -> list[dict]:
    """List WhatsApp chats with last message info."""
    try:
        async with aiosqlite.connect(_db_path()) as db:
            parts = [
                "SELECT c.jid, c.name, c.last_message_time, "
                "m.content, m.sender, m.is_from_me "
                "FROM chats c "
                "LEFT JOIN messages m ON c.jid = m.chat_jid "
                "AND c.last_message_time = m.timestamp"
            ]
            where = []
            params = []

            if query:
                where.append("(LOWER(c.name) LIKE LOWER(?) OR c.jid LIKE ?)")
                params.extend([f"%{query}%", f"%{query}%"])

            if where:
                parts.append("WHERE " + " AND ".join(where))
            parts.append("ORDER BY c.last_message_time DESC LIMIT ?")
            params.append(limit)

            cursor = await db.execute(" ".join(parts), tuple(params))
            rows = await cursor.fetchall()

            return [{
                "jid": row[0],
                "name": row[1],
                "last_message_time": row[2],
                "last_message": row[3],
                "last_sender": row[4],
                "is_group": row[0].endswith("@g.us") if row[0] else False,
            } for row in rows]
    except Exception as e:
        log.error(f"WA list_chats error: {e}")
        return []


async def search_contacts(query: str) -> list[dict]:
    """Search WA contacts by name or phone number."""
    try:
        async with aiosqlite.connect(_db_path()) as db:
            pattern = f"%{query}%"
            cursor = await db.execute(
                "SELECT DISTINCT jid, name FROM chats "
                "WHERE (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?)) "
                "AND jid NOT LIKE '%@g.us' "
                "ORDER BY name, jid LIMIT 50",
                (pattern, pattern),
            )
            rows = await cursor.fetchall()
            return [{
                "jid": row[0],
                "name": row[1],
                "phone": row[0].split("@")[0] if row[0] else "",
            } for row in rows]
    except Exception as e:
        log.error(f"WA search_contacts error: {e}")
        return []


async def get_new_messages_since(timestamp: str) -> list[dict]:
    """Get new incoming messages since a timestamp. Used by the polling loop.

    Only returns messages from allowed phones (not from the bot itself).
    """
    try:
        async with aiosqlite.connect(_db_path()) as db:
            cursor = await db.execute(
                "SELECT m.timestamp, m.sender, c.name, m.content, m.is_from_me, "
                "c.jid, m.id, m.media_type "
                "FROM messages m JOIN chats c ON m.chat_jid = c.jid "
                "WHERE m.timestamp > ? AND m.is_from_me = 0 "
                "ORDER BY m.timestamp ASC",
                (timestamp,),
            )
            rows = await cursor.fetchall()

            messages = []
            for row in rows:
                sender_jid = row[1]
                sender_id = sender_jid.split("@")[0] if "@" in sender_jid else sender_jid
                # Resolve LID to phone number if sender is a LID
                phone = sender_id
                if sender_id not in WA_ALLOWED_PHONES:
                    resolved = await _resolve_lid_to_phone(sender_id)
                    if resolved:
                        phone = resolved
                if phone not in WA_ALLOWED_PHONES:
                    continue
                sender_name = WA_ALLOWED_PHONES.get(phone)
                if not sender_name:
                    sender_name = await _get_sender_name(db, sender_jid)
                # Resolve chat_jid: if it's a @lid DM, map to phone@s.whatsapp.net
                effective_chat_jid = row[5]
                if "@lid" in effective_chat_jid:
                    effective_chat_jid = f"{phone}@s.whatsapp.net"

                messages.append({
                    "timestamp": row[0],
                    "sender": sender_jid,
                    "sender_name": sender_name,
                    "chat_name": row[2],
                    "content": row[3],
                    "chat_jid": effective_chat_jid,
                    "id": row[6],
                    "media_type": row[7],
                    "phone": phone,
                })
            return messages
    except Exception as e:
        log.error(f"WA get_new_messages_since error: {e}")
        return []


def format_messages_text(messages: list[dict]) -> str:
    """Format WA messages as readable text for tools."""
    if not messages:
        return "No messages found."
    lines = []
    for msg in messages:
        sender = "Me" if msg.get("is_from_me") else msg.get("sender_name", msg.get("sender", "?"))
        media_tag = f"[{msg['media_type']}] " if msg.get("media_type") else ""
        chat_info = f" (in {msg['chat_name']})" if msg.get("chat_name") else ""
        ts = msg.get("timestamp", "?")[:19]
        lines.append(f"[{ts}]{chat_info} {sender}: {media_tag}{msg.get('content', '')}")
    return "\n".join(lines)
