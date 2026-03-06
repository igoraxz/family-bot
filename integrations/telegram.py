"""Telegram Bot API integration — webhook + message sending."""

import asyncio
import logging
import os

import httpx

from config import TG_BOT_TOKEN, TG_CHAT_ID, TG_ALLOWED_USERS, TG_ALLOWED_CHATS, TG_BOT_USER_ID, TG_MCP_BOT_USER_ID, TMP_DIR, MAX_MEDIA_SIZE_MB

log = logging.getLogger(__name__)

API_BASE = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
FILE_BASE = f"https://api.telegram.org/file/bot{TG_BOT_TOKEN}"

_http: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=30.0)
    return _http


# === SENDING ===

async def _retry_on_429(method: str, payload: dict, max_retries: int = 2) -> dict | None:
    """Call TG API with retry on 429 (Too Many Requests)."""
    client = await get_client()
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(f"{API_BASE}/{method}", json=payload)
            data = resp.json()
            if data.get("ok"):
                return data["result"]
            # Handle rate limiting
            retry_after = data.get("parameters", {}).get("retry_after")
            if retry_after and attempt < max_retries:
                log.warning(f"{method} rate-limited, waiting {retry_after}s (attempt {attempt + 1})")
                await asyncio.sleep(min(retry_after, 30))
                continue
            return data  # Return error data for caller to handle
        except Exception as e:
            log.error(f"{method} error: {e}")
            return None
    return None


async def send_message(text: str, chat_id: int = TG_CHAT_ID,
                       parse_mode: str | None = "HTML") -> dict | None:
    """Send a text message. Returns the sent message dict or None."""
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    data = await _retry_on_429("sendMessage", payload)
    if isinstance(data, dict) and data.get("message_id"):
        return data
    if isinstance(data, dict) and not data.get("ok", True):
        log.error(f"sendMessage failed: {data}")
    return data if isinstance(data, dict) and data.get("message_id") else None


async def edit_message(message_id: int, text: str, chat_id: int = TG_CHAT_ID,
                       parse_mode: str | None = "HTML") -> dict | None:
    """Edit an existing message. Returns updated message dict or None."""
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    data = await _retry_on_429("editMessageText", payload, max_retries=1)
    if isinstance(data, dict) and data.get("message_id"):
        return data
    if isinstance(data, dict) and not data.get("ok", True):
        desc = str(data.get("description", ""))
        if "message is not modified" in desc:
            return None
        if "message to edit not found" in desc or "message can't be edited" in desc:
            raise RuntimeError(f"editMessageText: {desc}")
        log.error(f"editMessageText failed: {data}")
    return None


async def send_message_draft(text: str, chat_id: int = TG_CHAT_ID,
                             parse_mode: str | None = None) -> dict | None:
    """Send a streaming draft message (Bot API 9.3+).

    Call repeatedly with progressively longer text.
    Draft auto-finalizes when no more updates are sent.
    Returns the Message object or None if unsupported.
    """
    client = await get_client()
    try:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = await client.post(f"{API_BASE}/sendMessageDraft", json=payload)
        data = resp.json()
        if data.get("ok"):
            return data["result"]
        # API method not available — return None to trigger fallback
        if "not found" in str(data.get("description", "")).lower():
            return None
        return None
    except Exception:
        return None


async def delete_message(message_id: int, chat_id: int = TG_CHAT_ID) -> bool:
    """Delete a message."""
    client = await get_client()
    try:
        resp = await client.post(f"{API_BASE}/deleteMessage", json={
            "chat_id": chat_id,
            "message_id": message_id,
        })
        return resp.json().get("ok", False)
    except Exception:
        return False


async def send_chat_action(action: str = "typing", chat_id: int = TG_CHAT_ID):
    """Send typing indicator or other chat action."""
    client = await get_client()
    try:
        await client.post(f"{API_BASE}/sendChatAction", json={
            "chat_id": chat_id,
            "action": action,
        })
    except Exception:
        pass


async def send_photo(photo_path: str, caption: str = "",
                     chat_id: int = TG_CHAT_ID) -> dict | None:
    """Send a photo file."""
    client = await get_client()
    try:
        with open(photo_path, "rb") as f:
            files = {"photo": (os.path.basename(photo_path), f, "image/jpeg")}
            data = {"chat_id": str(chat_id), "parse_mode": "HTML"}
            if caption:
                data["caption"] = caption
            resp = await client.post(f"{API_BASE}/sendPhoto", data=data, files=files)
        result = resp.json()
        if result.get("ok"):
            return result["result"]
        log.error(f"sendPhoto failed: {result}")
        return None
    except Exception as e:
        log.error(f"sendPhoto error: {e}")
        return None


async def send_document(doc_path: str, caption: str = "",
                        chat_id: int = TG_CHAT_ID) -> dict | None:
    """Send a document file."""
    client = await get_client()
    try:
        with open(doc_path, "rb") as f:
            files = {"document": (os.path.basename(doc_path), f)}
            data = {"chat_id": str(chat_id), "parse_mode": "HTML"}
            if caption:
                data["caption"] = caption
            resp = await client.post(f"{API_BASE}/sendDocument", data=data, files=files)
        result = resp.json()
        if result.get("ok"):
            return result["result"]
        log.error(f"sendDocument failed: {result}")
        return None
    except Exception as e:
        log.error(f"sendDocument error: {e}")
        return None


# === WEBHOOK MANAGEMENT ===

async def set_webhook(url: str) -> bool:
    """Register webhook URL with Telegram."""
    client = await get_client()
    try:
        resp = await client.post(f"{API_BASE}/setWebhook", json={"url": url})
        data = resp.json()
        if data.get("ok"):
            log.info(f"Webhook set: {url}")
            return True
        log.error(f"setWebhook failed: {data}")
        return False
    except Exception as e:
        log.error(f"setWebhook error: {e}")
        return False


async def delete_webhook() -> bool:
    """Remove webhook (switch back to polling if needed)."""
    client = await get_client()
    try:
        resp = await client.post(f"{API_BASE}/deleteWebhook")
        data = resp.json()
        return data.get("ok", False)
    except Exception:
        return False


# === MEDIA DOWNLOAD ===

async def download_media(message: dict) -> dict | None:
    """Download photo or document from a TG message.

    Returns dict with: local_path, media_type, filename, file_size, and type-specific metadata.
    """
    msg_id = message.get("message_id", 0)
    file_id = None
    media_type = None
    filename = None
    meta = {}

    photo = message.get("photo")
    document = message.get("document")
    sticker = message.get("sticker")
    voice = message.get("voice")
    video_note = message.get("video_note")
    video = message.get("video")
    animation = message.get("animation")

    if photo:
        best = photo[-1]
        file_id = best.get("file_id")
        media_type = "photo"
        filename = f"{msg_id}_photo.jpg"
        meta["width"] = best.get("width", 0)
        meta["height"] = best.get("height", 0)
    elif document:
        file_id = document.get("file_id")
        media_type = "document"
        filename = document.get("file_name", f"{msg_id}_document")
        meta["mime_type"] = document.get("mime_type", "")
        file_size = document.get("file_size", 0)
        if file_size > MAX_MEDIA_SIZE_MB * 1024 * 1024:
            log.warning(f"Document too large ({file_size} bytes)")
            return None
    elif animation:
        file_id = animation.get("file_id")
        media_type = "animation"
        filename = f"{msg_id}_animation.mp4"
    elif video:
        file_id = video.get("file_id")
        media_type = "video"
        filename = f"{msg_id}_video.mp4"
        if video.get("file_size", 0) > MAX_MEDIA_SIZE_MB * 1024 * 1024:
            return None
    elif sticker:
        file_id = sticker.get("file_id")
        media_type = "sticker"
        filename = f"{msg_id}_sticker.webp"
        meta["emoji"] = sticker.get("emoji", "")
    elif voice:
        file_id = voice.get("file_id")
        media_type = "voice"
        filename = f"{msg_id}_voice.ogg"
        meta["duration"] = voice.get("duration", 0)
    elif video_note:
        file_id = video_note.get("file_id")
        media_type = "video_note"
        filename = f"{msg_id}_videonote.mp4"
    elif message.get("audio"):
        audio = message["audio"]
        file_id = audio.get("file_id")
        media_type = "audio"
        filename = audio.get("file_name", f"{msg_id}_audio.mp3")
        meta["duration"] = audio.get("duration", 0)
        meta["title"] = audio.get("title", "")
        meta["performer"] = audio.get("performer", "")
        meta["mime_type"] = audio.get("mime_type", "")
        if audio.get("file_size", 0) > MAX_MEDIA_SIZE_MB * 1024 * 1024:
            return None
    else:
        return None

    if not file_id:
        return None

    client = await get_client()
    try:
        resp = await client.get(f"{API_BASE}/getFile", params={"file_id": file_id})
        data = resp.json()
        if not data.get("ok"):
            return None
        file_path_remote = data["result"]["file_path"]
    except Exception as e:
        log.error(f"getFile error: {e}")
        return None

    local_path = str(TMP_DIR / filename)
    try:
        resp = await client.get(f"{FILE_BASE}/{file_path_remote}", timeout=60.0)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            f.write(resp.content)
        file_size_dl = len(resp.content)
        log.info(f"Downloaded {media_type}: {filename} ({file_size_dl} bytes)")
    except Exception as e:
        log.error(f"Download failed: {e}")
        return None

    return {
        "local_path": local_path,
        "media_type": media_type,
        "filename": filename,
        "file_size": file_size_dl,
        **meta,
    }


# === PARSING ===

def parse_update(update: dict) -> dict | None:
    """Parse a Telegram update into a normalized message dict.

    Returns None if the message should be ignored (wrong chat, unauthorized, bot's own, etc).
    Returns dict with: source, user_id, user_name, text, message_id, media_info, raw.
    """
    message = update.get("message", {})
    if not message:
        return None

    from_user = message.get("from", {})
    user_id = from_user.get("id", 0)
    chat_id = message.get("chat", {}).get("id", 0)
    text = message.get("text", "") or message.get("caption", "")
    is_bot = from_user.get("is_bot", False)
    has_media = any(message.get(k) for k in (
        "photo", "document", "sticker", "voice", "video_note", "video", "animation", "audio",
    ))
    has_location = bool(message.get("location") or message.get("venue"))
    has_contact = bool(message.get("contact"))

    # Not in allowed chats — stay completely silent
    if chat_id not in TG_ALLOWED_CHATS:
        return None

    # No content
    if not text and not has_media and not has_location and not has_contact:
        return None

    # Bot's own messages — store for context but don't process
    if is_bot or user_id in (TG_BOT_USER_ID, TG_MCP_BOT_USER_ID):
        return {
            "source": "telegram",
            "is_bot_message": True,
            "user_name": from_user.get("first_name", "Bot"),
            "text": text or "[media]",
        }

    # Unauthorized user in allowed chat — still silent
    if user_id not in TG_ALLOWED_USERS:
        log.warning(f"Ignoring TG from unauthorized user {user_id}")
        return None

    # Extract reply-to context (for cancel-by-reply and forwarded context)
    reply_to = message.get("reply_to_message")
    reply_context = None
    if reply_to:
        reply_from = reply_to.get("from", {})
        reply_text = reply_to.get("text", "") or reply_to.get("caption", "")
        reply_has_media = any(reply_to.get(k) for k in (
            "photo", "document", "sticker", "voice", "video_note", "video", "animation", "audio",
        ))
        # Extract contact from reply-to
        reply_contact = None
        if reply_to.get("contact"):
            rc = reply_to["contact"]
            reply_contact = {
                "phone_number": rc.get("phone_number", ""),
                "first_name": rc.get("first_name", ""),
                "last_name": rc.get("last_name", ""),
            }
        # Extract location/venue from reply-to message
        reply_location = None
        if reply_to.get("venue"):
            v = reply_to["venue"]
            rl = v.get("location", {})
            reply_location = {
                "latitude": rl.get("latitude"),
                "longitude": rl.get("longitude"),
                "title": v.get("title", ""),
                "address": v.get("address", ""),
            }
        elif reply_to.get("location"):
            rl = reply_to["location"]
            reply_location = {
                "latitude": rl.get("latitude"),
                "longitude": rl.get("longitude"),
                "live_period": rl.get("live_period"),
            }
        reply_context = {
            "message_id": reply_to.get("message_id", 0),
            "user_id": reply_from.get("id", 0),
            "is_bot": reply_from.get("is_bot", False),
            "text": reply_text[:500],
            "has_media": reply_has_media,
            "location": reply_location,
            "contact": reply_contact,
            "raw": reply_to if reply_has_media else None,
        }

    # Extract forward context
    forward_context = None
    forward_origin = message.get("forward_origin") or {}
    if forward_origin:
        forward_context = {
            "type": forward_origin.get("type", "unknown"),
            "date": forward_origin.get("date", 0),
        }
        if forward_origin.get("sender_user"):
            su = forward_origin["sender_user"]
            forward_context["sender_name"] = su.get("first_name", "")
    elif message.get("forward_from"):
        ff = message["forward_from"]
        forward_context = {
            "type": "user",
            "sender_name": ff.get("first_name", ""),
            "sender_id": ff.get("id", 0),
        }

    # Extract location/venue from direct message
    location_data = None
    if message.get("venue"):
        v = message["venue"]
        loc = v.get("location", {})
        location_data = {
            "latitude": loc.get("latitude"),
            "longitude": loc.get("longitude"),
            "title": v.get("title", ""),
            "address": v.get("address", ""),
        }
    elif message.get("location"):
        loc = message["location"]
        location_data = {
            "latitude": loc.get("latitude"),
            "longitude": loc.get("longitude"),
            "live_period": loc.get("live_period"),  # present for live locations
        }

    # Extract contact info from direct message
    contact_data = None
    if message.get("contact"):
        c = message["contact"]
        contact_data = {
            "phone_number": c.get("phone_number", ""),
            "first_name": c.get("first_name", ""),
            "last_name": c.get("last_name", ""),
            "vcard": c.get("vcard", ""),
        }

    return {
        "source": "telegram",
        "is_bot_message": False,
        "user_id": user_id,
        "user_name": TG_ALLOWED_USERS[user_id],
        "text": text,
        "message_id": message.get("message_id", 0),
        "chat_id": chat_id,
        "has_media": has_media,
        "location": location_data,
        "contact": contact_data,
        "reply_to": reply_context,
        "forward": forward_context,
        "raw": message,
    }
