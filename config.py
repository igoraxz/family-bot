"""Configuration for Family Bot (Claude Agent SDK).

Loads family-specific data from family_config.json (mounted as volume).
All secrets come from environment variables (.env file).
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# === PATHS ===
BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
PROMPTS_DIR = DATA_DIR / "prompts"
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "conversations.db"
KNOWLEDGE_FILE = DATA_DIR / "family_knowledge.md"
FACTS_FILE = DATA_DIR / "family_facts.json"
GOALS_FILE = DATA_DIR / "family_goals.json"
GOOGLE_WORKSPACE_CREDS_DIR = DATA_DIR / "google-workspace-creds"
MEDIA_CACHE_DIR = DATA_DIR / "media_cache"

# Ensure directories exist
for d in [DATA_DIR, PROMPTS_DIR, TMP_DIR, GOOGLE_WORKSPACE_CREDS_DIR, MEDIA_CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# === FAMILY CONFIG (loaded from JSON file) ===
FAMILY_CONFIG_PATH = Path(os.environ.get("FAMILY_CONFIG_PATH", DATA_DIR / "family_config.json"))

def _load_family_config() -> dict:
    """Load family_config.json. Returns empty dict if missing."""
    if FAMILY_CONFIG_PATH.exists():
        try:
            return json.loads(FAMILY_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Failed to load family config from {FAMILY_CONFIG_PATH}: {e}")
    return {}

_family_cfg = _load_family_config()

# Family identity
FAMILY_NAME = _family_cfg.get("family_name", "")
BOT_NAME = _family_cfg.get("bot_name", "Family Bot")
FAMILY_TIMEZONE = _family_cfg.get("timezone", "Europe/London")

# Build FAMILY_CONTEXT from config (used in system prompt)
def _build_family_context() -> dict:
    """Build the FAMILY_CONTEXT dict from family_config.json."""
    members = _family_cfg.get("members", {})
    parents = members.get("parents", [])
    children = members.get("children", [])
    other = members.get("other", [])

    ctx: dict = {"family": {}, "location": _family_cfg.get("location", "")}

    for p in parents:
        role = p.get("role", "parent")
        entry: dict = {"name": p.get("name", "")}
        if "email" in p:
            entry["email"] = p["email"]
        if "emails" in p:
            entry["emails"] = p["emails"]
        if "telegram_id" in p:
            entry["telegram_id"] = p["telegram_id"]
        if "telegram_username" in p:
            entry["telegram_username"] = p["telegram_username"]
        ctx["family"][role] = entry

    if children:
        ctx["family"]["children"] = children

    for o in other:
        role = o.get("role", "other")
        ctx[role] = {"name": o.get("name", "")}

    return ctx

FAMILY_CONTEXT = _build_family_context()

# Extract parent info for convenience
_parents = _family_cfg.get("members", {}).get("parents", [])

def _get_parent_names() -> list[str]:
    return [p.get("name", "").split()[0] for p in _parents if p.get("name")]

PARENT_NAMES = _get_parent_names()

# Primary email (for sending)
_email_cfg = _family_cfg.get("email", {})
PRIMARY_EMAIL = _email_cfg.get("primary_address", "")
PRIMARY_EMAIL_USER = _email_cfg.get("primary_user_name", "")

# Phone agent config
_phone_cfg = _family_cfg.get("phone_agent", {})
PHONE_FAMILY_SURNAME = _phone_cfg.get("family_surname", FAMILY_NAME)
PHONE_DEFAULT_GENDER = _phone_cfg.get("default_gender", "male")

# Goals
DEFAULT_GOALS = _family_cfg.get("goals", [
    "Plan family time effectively — weekends, holidays, outings",
    "Keep everyone happy and reduce stress",
    "Support children's education and extracurricular activities",
    "Stay on top of school events, deadlines, and applications",
])

# Build user tag map for prompts (telegram deep links)
def _build_user_tags() -> dict[str, str]:
    """Build {Name: '<a href=\"tg://user?id=...\">Name</a>'} for prompt templates."""
    tags = {}
    for p in _parents:
        name = p.get("name", "").split()[0]
        tg_id = p.get("telegram_id")
        if name and tg_id:
            tags[name] = f'<a href="tg://user?id={tg_id}">{name}</a>'
    return tags

USER_TAGS = _build_user_tags()

# Build authorized user descriptions for prompts
def _build_authorized_users_desc() -> str:
    """Build a description like 'John (123456789) and Jane (987654321)'."""
    parts = []
    for p in _parents:
        name = p.get("name", "").split()[0]
        tg_id = p.get("telegram_id")
        if name and tg_id:
            parts.append(f"{name} ({tg_id})")
    return " and ".join(parts)

AUTHORIZED_USERS_DESC = _build_authorized_users_desc()

# Build WA authorized users description for prompts
def _build_wa_authorized_desc() -> str:
    """Build WA authorized users lines for the whatsapp prompt."""
    lines = []
    for p in _parents:
        name = p.get("name", "").split()[0]
        phone = p.get("whatsapp_phone", "")
        if name and phone:
            lines.append(f"  - {name}: +{phone} (JID: {phone}@s.whatsapp.net)")
    return "\n".join(lines)

WA_AUTHORIZED_DESC = _build_wa_authorized_desc()

# Build reply tag rules for prompts
def _build_reply_tag_rules() -> str:
    """Build reply tag rules like 'John: <a href=...>John</a>'."""
    lines = []
    for p in _parents:
        name = p.get("name", "").split()[0]
        tg_id = p.get("telegram_id")
        if name and tg_id:
            lines.append(f'  {name}: <a href="tg://user?id={tg_id}">{name}</a>')
    return "\n".join(lines)

REPLY_TAG_RULES = _build_reply_tag_rules()

# Build children summary for identity prompt
def _build_members_summary() -> str:
    """Build 'Name1, Name2, and their N children (Child1, Child2, ...)'."""
    parent_names = PARENT_NAMES
    children = _family_cfg.get("members", {}).get("children", [])
    child_names = [c.get("name", "") for c in children if c.get("name")]

    parts = []
    if parent_names:
        parts.append(", ".join(parent_names))
    if child_names:
        parts.append(f"and their {'child' if len(child_names) == 1 else f'{len(child_names)} children'} ({', '.join(child_names)})")
    return " ".join(parts)

MEMBERS_SUMMARY = _build_members_summary()

# === API KEYS ===
CLAUDE_CODE_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# === TELEGRAM ===
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = int(os.environ.get("TG_CHAT_ID", "0"))
TG_WEBHOOK_URL = os.environ.get("TG_WEBHOOK_URL", "")
TG_BOT_USER_ID = int(os.environ.get("TG_BOT_USER_ID", "0"))
TG_MCP_BOT_USER_ID = int(os.environ.get("TG_MCP_BOT_USER_ID", "0"))

# Security: only these user IDs can trigger inference (from env)
_tg_users_str = os.environ.get("TG_ALLOWED_USERS", "")  # format: "123456789:John,987654321:Jane"
TG_ALLOWED_USERS = {}
for _pair in _tg_users_str.split(","):
    if ":" in _pair:
        _uid, _name = _pair.strip().split(":", 1)
        TG_ALLOWED_USERS[int(_uid)] = _name

# Auto-populate from family config if env var was empty
if not TG_ALLOWED_USERS:
    for p in _parents:
        tg_id = p.get("telegram_id")
        name = p.get("name", "").split()[0]
        if tg_id and name:
            TG_ALLOWED_USERS[tg_id] = name

# Chats where the bot is allowed to respond (silent in all others)
# Include group chat + all authorized users' private chats (chat_id == user_id in DMs)
TG_ALLOWED_CHATS = {TG_CHAT_ID} if TG_CHAT_ID else set()
for _uid in TG_ALLOWED_USERS:
    TG_ALLOWED_CHATS.add(_uid)

# === WHATSAPP ===
WA_BRIDGE_URL = os.environ.get("WA_BRIDGE_URL", "http://localhost:8081")
WA_API_URL = os.environ.get("WA_API_URL", f"{WA_BRIDGE_URL}/api")
WA_DB_PATH = Path(os.environ.get("WA_DB_PATH", BASE_DIR.parent / "whatsapp-mcp" / "whatsapp-bridge" / "store" / "messages.db"))
WA_BOT_PHONE = os.environ.get("WA_BOT_PHONE", "")
WA_FAMILY_GROUP_JID = os.environ.get("WA_FAMILY_GROUP_JID", "")
_wa_phones_str = os.environ.get("WA_ALLOWED_PHONES", "")  # format: "447700900001:John,447700900002:Jane"
WA_ALLOWED_PHONES = {}
for _pair in _wa_phones_str.split(","):
    if ":" in _pair:
        _phone, _name = _pair.strip().split(":", 1)
        WA_ALLOWED_PHONES[_phone] = _name

# Auto-populate from family config if env var was empty
if not WA_ALLOWED_PHONES:
    for p in _parents:
        phone = p.get("whatsapp_phone", "")
        name = p.get("name", "").split()[0]
        if phone and name:
            WA_ALLOWED_PHONES[phone] = name

# Chats where the bot responds (silent in all other WA chats)
# Include group chat + all authorized users' private chats (chat_jid == phone@s.whatsapp.net in DMs)
WA_ALLOWED_CHATS = {WA_FAMILY_GROUP_JID} if WA_FAMILY_GROUP_JID else set()
for _phone in WA_ALLOWED_PHONES:
    WA_ALLOWED_CHATS.add(f"{_phone}@s.whatsapp.net")
WA_POLL_INTERVAL = 1  # seconds between WA polls (fast for responsive replies)

# === VAPI (Phone Calls) ===
VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "")
VAPI_PHONE_NUMBER_ID = os.environ.get("VAPI_PHONE_NUMBER_ID", "")
VAPI_VOICE_ID = os.environ.get("VAPI_VOICE_ID", "")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "")

# === ADMIN USERS (can use self-upgrade tools, from env) ===
_admin_str = os.environ.get("ADMIN_USERS", "")  # format: "telegram:123456789,whatsapp:447700900001"
ADMIN_USERS = set()
for _pair in _admin_str.split(","):
    if ":" in _pair:
        _source, _uid = _pair.strip().split(":", 1)
        ADMIN_USERS.add((_source, _uid))

# Auto-populate admins from family config if env var was empty
if not ADMIN_USERS:
    for p in _parents:
        if p.get("is_admin"):
            tg_id = p.get("telegram_id")
            phone = p.get("whatsapp_phone")
            if tg_id:
                ADMIN_USERS.add(("telegram", str(tg_id)))
            if phone:
                ADMIN_USERS.add(("whatsapp", phone))

# === CLAUDE AGENT SDK CONFIG ===
MODEL_QUICK = "claude-sonnet-4-6"
MODEL_LONG = "claude-opus-4-6"

# Agent loop limits
MAX_TURNS = 75  # max tool-call iterations per query
MAX_TOOL_RESULT_CHARS = 30000  # truncate tool results above this
TOOL_TIMEOUT = 90  # seconds per individual tool execution

# === CONTEXT MANAGEMENT ===
SESSION_TIMEOUT = 86400  # seconds (24h)

# === RAG (Retrieval-Augmented Generation) ===
RAG_CHUNK_SIZE = 7    # messages per chunk (window size for semantic search)
RAG_CHUNK_STRIDE = 3  # slide forward by N messages between chunks
RAG_EMBEDDING_MODEL = "gemini-embedding-001"
RAG_EMBEDDING_DIM = 3072
RAG_EMBEDDING_BATCH_SIZE = 100  # Gemini supports up to 100 texts per batch

# === BOT BEHAVIOR ===
PROACTIVE_HOUR = int(os.environ.get("PROACTIVE_HOUR", "7"))
PROACTIVE_MINUTE = int(os.environ.get("PROACTIVE_MINUTE", "10"))
EMAIL_POLL_INTERVAL_H = int(os.environ.get("EMAIL_POLL_INTERVAL_H", "3"))
STREAM_EDIT_INTERVAL = 3.0  # seconds between streaming edits (TG rate limit ~20/min)
MAX_MEDIA_SIZE_MB = 20
MEDIA_RETENTION_DAYS = int(os.environ.get("MEDIA_RETENTION_DAYS", "30"))

# === TRIAGE: QUICK vs LONG ===
QUICK_TIMEOUT = 30
LONG_STATUS_INTERVAL = 180
LONG_SOFT_TIMEOUT = 600
MAX_PARALLEL_TASKS = 3  # max concurrent tasks per chat

# Effort levels for router
EFFORT_SIMPLE = "low"
EFFORT_MEDIUM = "medium"
EFFORT_COMPLEX = "high"
EFFORT_MAX = "max"

# === WEB SERVER ===
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
