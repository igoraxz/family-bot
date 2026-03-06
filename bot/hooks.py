"""Agent SDK hooks — admin gating, file protection, tool status notifications.

Hooks fire at key execution points in the SDK agent loop:
- PreToolUse: before tool execution (can deny/modify)
- PostToolUse: after tool completes (for status updates)
- Stop: when agent finishes (for cleanup)

SECURITY MODEL:
- Container isolation is the PRIMARY boundary (Docker, non-root user)
- These hooks are DEFENSE-IN-DEPTH — they block sensitive operations even if
  prompt injection convinces the model to call dangerous tools
- Admin-only tools (Bash, Write, Edit) are gated by user context
- File paths are checked against blocklists for credential files
- Bash commands are checked against a blocklist (container is already sandboxed)
"""

import contextvars
import json
import logging
import re
from typing import Callable, Optional

from config import ADMIN_USERS

log = logging.getLogger(__name__)

# Context variable: set before each query, readable by hooks
current_user_ctx: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    'current_user', default=None
)

# Callback for tool status updates (set per-request by main.py)
tool_status_callback: contextvars.ContextVar[Optional[Callable]] = contextvars.ContextVar(
    'tool_status_callback', default=None
)

# Track whether a send tool was called (for fallback send logic)
send_tool_called: contextvars.ContextVar[bool] = contextvars.ContextVar(
    'send_tool_called', default=False
)

# Track intentional silence (e.g. message was for another user, not the bot)
intentional_silence: contextvars.ContextVar[bool] = contextvars.ContextVar(
    'intentional_silence', default=False
)

# Tools that constitute a "reply sent" (bare names — matched after stripping MCP namespace)
_SEND_TOOLS = frozenset({
    "telegram_send_message", "telegram_send_photo", "telegram_send_document",
    "whatsapp_send_message", "whatsapp_send_file",  # Custom WA tools
    "send_message", "send_file",  # External WhatsApp MCP tools (bare names, if ever used)
})

# Friendly labels for tool status (bare names — matched after stripping MCP namespace)
_TOOL_LABELS = {
    # SDK built-in tools
    "WebSearch": "Searching web...",
    "WebFetch": "Fetching page...",
    # Google Workspace MCP tools
    "search_gmail_messages": "Checking email...",
    "get_gmail_message_content": "Reading email...",
    "send_gmail_message": "Sending email...",
    "get_gmail_attachment_content": "Downloading attachment...",
    "get_events": "Checking calendar...",
    "manage_event": "Managing event...",
    "search_memory": "Searching memory...",
    "get_recent_conversation": "Loading history...",
    "phone_call": "Making call...",
    # Playwright MCP tools
    "browser_navigate": "Opening browser...",
    "browser_snapshot": "Reading page...",
    "browser_screenshot": "Taking screenshot...",
    # WhatsApp custom tools
    "whatsapp_list_messages": "Reading WhatsApp...",
    "whatsapp_list_chats": "Listing WA chats...",
    "whatsapp_search_contacts": "Searching WA contacts...",
    # WhatsApp external MCP tools (if ever used via ToolSearch)
    "list_messages": "Reading WhatsApp...",
    "search_contacts": "Searching contacts...",
}

# === SECURITY: File path protection ===

# Patterns that MUST NOT appear anywhere in a file path (case-insensitive)
_BLOCKED_PATH_PATTERNS = {
    ".env", "credentials", "token", "secret",
    "gmail-api/", "google-workspace-creds/",
    ".ssh/", "id_rsa", "id_ed25519",
    "/etc/shadow", "/etc/passwd",
    ".docker/config",
}
_BLOCKED_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".jks"}

# === SECURITY: Bash command restrictions ===

# Admin-only tools that require authorization
_ADMIN_ONLY_TOOLS = frozenset({"Bash", "Write", "Edit", "deploy_bot", "deploy_status"})

# Bash uses BLOCKLIST-ONLY approach: everything is allowed EXCEPT dangerous patterns.
# Rationale: container is already sandboxed (Docker, non-root, no secrets in image).
# The blocklist prevents secret exfiltration and destructive host-level operations.
_BASH_BLOCKLIST = [
    # --- Secret exfiltration (env vars) ---
    re.compile(r"\benv\b"),           # dump all env vars (contains tokens)
    re.compile(r"\bprintenv\b"),      # dump env vars
    re.compile(r"\bset\b(?!\s+-e)"),  # dump shell vars (but allow set -e)
    re.compile(r"\bexport\b"),        # export vars
    re.compile(r"\.env\b"),           # any reference to .env files
    re.compile(r"os\.environ"),       # python os.environ access
    re.compile(r"process\.env"),      # node process.env access
    # --- Secret exfiltration (files) ---
    re.compile(r"\b(cat|head|tail|less|more|strings|xxd|base64|od)\b.*\.(env|pem|key|secret)"),
    re.compile(r"\b(cat|head|tail|less|more|strings|xxd|base64|od)\b.*\.credentials"),
    re.compile(r"\bpython.*open\(.*\.(env|pem|key)"),  # python file reading of secrets
    # --- Network exfiltration ---
    re.compile(r"\bwget\b"),          # no wget
    re.compile(r"\bnc\b|\bnetcat\b"), # no netcat
    re.compile(r"\bssh\b(?!-keyscan)"),  # no ssh (except keyscan)
    re.compile(r"\bcurl\b(?!.*localhost).*-[dX]"),  # curl POST/data to non-localhost
    re.compile(r"\bcurl\b.*@"),       # curl with file upload (@file)
    # --- Docker secrets ---
    re.compile(r"\bdocker\b.*\binspect\b"),  # reveals env vars in container config
    re.compile(r"\bdocker\b.*\bexec\b.*\b(env|printenv|set)\b"),  # env dump via docker exec
    # --- Privilege escalation ---
    re.compile(r"\bchmod\b.*\+s"),    # no setuid
    re.compile(r"\bsudo\b"),          # no sudo
    re.compile(r"\bsu\b\s"),          # no su
    re.compile(r"\bpasswd\b"),        # no passwd
    re.compile(r"\buseradd\b|\busermod\b"),  # no user management
    # --- System damage ---
    re.compile(r"/proc/|/sys/"),      # no proc/sys access
    re.compile(r"\brm\s+-rf\s+/"),    # no rm -rf /
]


def _extract_tool_name(tool_input: dict) -> str:
    """Extract the bare tool name from an MCP-namespaced tool name."""
    name = tool_input.get("tool_name", "")
    # MCP tools are namespaced: mcp__family-messaging__telegram_send_message
    parts = name.split("__")
    return parts[-1] if parts else name


def _is_admin() -> bool:
    """Check if the current user is an admin."""
    ctx = current_user_ctx.get()
    return ctx is not None and ctx.get("is_admin", False)


def _is_system_task() -> bool:
    """Check if this is a system-initiated task (no user context)."""
    return current_user_ctx.get() is None


def _check_file_path(file_path: str) -> str | None:
    """Check if a file path is allowed. Returns denial reason or None if OK."""
    path_lower = file_path.lower()
    for pat in _BLOCKED_PATH_PATTERNS:
        if pat in path_lower:
            return f"Access denied: path contains '{pat}'"
    for ext in _BLOCKED_EXTENSIONS:
        if path_lower.endswith(ext):
            return f"Access denied: blocked file extension '{ext}'"
    return None


_SECRET_VAR_WORDS = frozenset({"token", "key", "secret", "password", "credential"})
_SECRET_VAR_RE = re.compile(r"\$\{?(\w+)\}?")


def _check_bash_command(command: str) -> str | None:
    """Check if a Bash command is allowed. Returns denial reason or None if OK.

    Uses blocklist-only: everything is allowed except dangerous patterns.
    Container isolation (Docker, non-root) is the primary security boundary.
    """
    cmd_lower = command.lower().strip()

    for pattern in _BASH_BLOCKLIST:
        if pattern.search(cmd_lower):
            return "Access denied: command matches security blocklist"

    # Check for $VAR references to secret-sounding variables (e.g. echo $TOKEN)
    for m in _SECRET_VAR_RE.finditer(cmd_lower):
        varname = m.group(1)
        if any(sw in varname for sw in _SECRET_VAR_WORDS):
            return "Access denied: references secret environment variable"

    return None


def _deny(reason: str) -> dict:
    """Build a PreToolUse denial response."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


async def pre_tool_use_hook(input_data: dict, tool_use_id: str, context: dict) -> dict:
    """PreToolUse hook: admin gating + file protection + bash restrictions.

    Security layers (defense-in-depth):
    1. Admin-only tools (Bash, Write, Edit) require admin user context
    2. File paths checked against blocklist for credential/secret files
    3. Bash commands validated against blocklist of dangerous patterns
    4. Container isolation (Docker) is the ultimate boundary
    """
    bare_name = _extract_tool_name(input_data)
    tool_input = input_data.get("tool_input", {})

    # --- Layer 1: Admin gating for dangerous tools ---
    if bare_name in _ADMIN_ONLY_TOOLS:
        if not _is_admin() and not _is_system_task():
            log.warning(f"Non-admin tried to use {bare_name}: user={current_user_ctx.get()}")
            return _deny(f"Access denied: {bare_name} is admin-only")

    # --- Layer 2: File path protection ---
    if bare_name in ("Write", "Edit", "Read"):
        file_path = tool_input.get("file_path", "")
        denial = _check_file_path(file_path)
        if denial:
            log.warning(f"Blocked file access: {bare_name} → {file_path}")
            return _deny(denial)

    # --- Layer 3: Bash command restrictions ---
    if bare_name == "Bash":
        command = tool_input.get("command", "")
        denial = _check_bash_command(command)
        if denial:
            log.warning(f"Blocked bash command: {command[:100]}")
            return _deny(denial)

    return {}


async def post_tool_use_hook(input_data: dict, tool_use_id: str, context: dict) -> dict:
    """PostToolUse hook: track send tools + emit status updates."""
    bare_name = _extract_tool_name(input_data)

    # Track if a send tool was called
    if bare_name in _SEND_TOOLS:
        send_tool_called.set(True)

    # Emit tool status label (for streaming placeholder updates)
    if bare_name in _TOOL_LABELS and bare_name not in _SEND_TOOLS:
        callback = tool_status_callback.get()
        if callback:
            try:
                await callback([bare_name])
            except Exception as e:
                log.warning(f"Tool status callback error: {e}")

    return {}


def set_user_context(source: str, user_id: str, user_name: str):
    """Set the current user context for hook inspection."""
    is_admin = (source, str(user_id)) in ADMIN_USERS
    log.info(f"Admin check: ({source}, {user_id!r}) admin={is_admin}")
    current_user_ctx.set({
        "source": source,
        "user_id": str(user_id),
        "user_name": user_name,
        "is_admin": is_admin,
    })
    send_tool_called.set(False)


def build_hooks() -> dict:
    """Build the hooks dict for ClaudeAgentOptions.

    Returns a dict mapping hook event names to lists of HookMatcher objects.
    """
    try:
        from claude_agent_sdk import HookMatcher
        return {
            "PreToolUse": [
                HookMatcher(hooks=[pre_tool_use_hook]),
            ],
            "PostToolUse": [
                HookMatcher(hooks=[post_tool_use_hook]),
            ],
        }
    except ImportError:
        log.warning("claude_agent_sdk not available — hooks disabled")
        return {}
