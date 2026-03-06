"""External MCP server configuration for Claude Agent SDK.

Configures MCP servers that run as subprocesses (stdio transport):
  - playwright: Browser automation (navigate, screenshot, click, type, etc.)
  - whatsapp: WhatsApp messaging via WA bridge (send, list, search, etc.)
  - google-workspace: Gmail + Calendar via google_workspace_mcp

SDK built-in tools (Read, Write, Edit, Glob, Grep, Bash, WebFetch, WebSearch)
are always available — no configuration needed.
"""

import logging
import os

log = logging.getLogger(__name__)


def get_external_mcp_servers() -> dict:
    """Return external MCP server configs for ClaudeAgentOptions.

    Each entry is a dict with 'command', 'args', and optional 'env'
    matching the Claude Code MCP server config format.
    """
    servers = {}

    # --- Playwright MCP (browser automation) ---
    # Provides: browser_navigate, browser_screenshot, browser_snapshot,
    #   browser_click, browser_type, browser_select_option, browser_press_key,
    #   browser_tab_list, browser_pdf_save, etc.
    # Requires: Node.js + npx (installed in Dockerfile)
    servers["playwright"] = {
        "command": "npx",
        "args": [
            "-y", "@playwright/mcp@0.0.68",
            "--headless",
            "--browser", "chromium",
            "--user-data-dir", "/app/data/browser-profile",
            "--viewport-size", "1920x1080",
            "--image-responses", "omit",
            "--output-dir", "/app/data/tmp",
        ],
    }

    # --- WhatsApp MCP (messaging via WA bridge) ---
    # Provides: send_message, send_file, send_audio_message, send_location,
    #   list_messages, list_chats, search_contacts, get_chat,
    #   get_direct_chat_by_contact, get_contact_chats, get_last_interaction,
    #   get_message_context, download_media
    # Requires: Python + WA MCP server code + WA bridge running
    wa_db_path = os.environ.get("WA_DB_PATH", "/app/wa-data/messages.db")
    wa_bridge_url = os.environ.get("WA_BRIDGE_URL", "http://whatsapp-bridge:8080")
    # Extract port from bridge URL for the MCP server
    wa_port = wa_bridge_url.rstrip("/").split(":")[-1]

    servers["whatsapp"] = {
        "command": "python",
        "args": ["/app/whatsapp-mcp-server/main.py"],
        "env": {
            "WHATSAPP_DB_PATH": wa_db_path,
            "WHATSAPP_API_PORT": wa_port,
            "WHATSAPP_API_BASE_URL": f"{wa_bridge_url}/api",
        },
    }

    # --- Google Workspace MCP (Gmail + Calendar) ---
    # Provides: search_gmail_messages, get_gmail_message_content, send_gmail_message,
    #   draft_gmail_message, get_gmail_attachment_content, list_gmail_labels,
    #   manage_gmail_label, get_gmail_thread_content, modify_gmail_message_labels,
    #   get_events, manage_event, list_calendars, query_freebusy
    # Requires: workspace-mcp pip package + pre-authorized Google OAuth credentials
    # Credentials stored at /app/data/google-workspace-creds/<email>.json
    # Generated via: python scripts/setup_google_credentials.py (run locally)
    creds_dir = os.environ.get(
        "WORKSPACE_MCP_CREDENTIALS_DIR",
        "/app/data/google-workspace-creds",
    )
    # Client secrets can be passed via env vars or file
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")

    workspace_env = {
        "WORKSPACE_MCP_CREDENTIALS_DIR": creds_dir,
    }
    if client_id and client_secret:
        workspace_env["GOOGLE_OAUTH_CLIENT_ID"] = client_id
        workspace_env["GOOGLE_OAUTH_CLIENT_SECRET"] = client_secret

    servers["google-workspace"] = {
        "command": "workspace-mcp",
        "args": [
            "--tools", "gmail", "calendar",
            "--transport", "stdio",
            "--single-user",
        ],
        "env": workspace_env,
    }

    log.info(f"External MCP servers configured: {list(servers.keys())}")
    return servers
