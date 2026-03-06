#!/usr/bin/env python3
"""Generate Google OAuth credentials in workspace-mcp format.

Run this LOCALLY (not on server) — it opens a browser for OAuth consent.

Usage:
    pip install google-auth-oauthlib google-api-python-client
    python scripts/setup_google_credentials.py

It will:
1. Read your existing client_secret from data/gmail-api/gmail_credentials.json
2. Open a browser for OAuth consent with Gmail + Calendar scopes
3. Save credentials in workspace-mcp format at data/google-workspace-creds/<email>.json
4. These creds are mounted into Docker for the Google Workspace MCP server

If you already have credentials, it will refresh them instead of re-authorizing.
"""

import json
import os
import sys
from pathlib import Path

# Scopes needed for Gmail + Calendar (workspace-mcp format)
SCOPES = [
    # Base (required by workspace-mcp for user identification)
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
    # Gmail
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
    # Calendar
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    # Find project root
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    creds_file = project_root / "data" / "gmail-api" / "gmail_credentials.json"
    output_dir = project_root / "data" / "google-workspace-creds"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not creds_file.exists():
        print(f"ERROR: Client secrets file not found at {creds_file}")
        print("Download it from Google Cloud Console > APIs > Credentials > OAuth 2.0 Client IDs")
        sys.exit(1)

    # Load client secrets
    with open(creds_file) as f:
        client_config = json.load(f)

    # Check for existing credentials
    existing_creds = list(output_dir.glob("*.json"))
    if existing_creds:
        print(f"Found existing credentials: {existing_creds[0].name}")
        with open(existing_creds[0]) as f:
            creds_data = json.load(f)
        creds = Credentials(
            token=creds_data.get("token"),
            refresh_token=creds_data.get("refresh_token"),
            token_uri=creds_data.get("token_uri"),
            client_id=creds_data.get("client_id"),
            client_secret=creds_data.get("client_secret"),
            scopes=creds_data.get("scopes"),
        )
        if creds.refresh_token:
            print("Refreshing existing token...")
            creds.refresh(Request())
            _save_creds(existing_creds[0], creds)
            print(f"Token refreshed and saved to {existing_creds[0]}")
            return

    # Run OAuth flow
    print("Starting OAuth flow — a browser window will open for consent.")
    print(f"Requesting scopes: Gmail (read/send/modify/labels) + Calendar (full)")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0)

    # Get user email for filename
    service = build("oauth2", "v2", credentials=creds)
    user_info = service.userinfo().get().execute()
    email = user_info.get("email", "unknown")
    print(f"\nAuthenticated as: {email}")

    # Save in workspace-mcp format
    output_file = output_dir / f"{email}.json"
    _save_creds(output_file, creds)
    print(f"\nCredentials saved to: {output_file}")
    print(f"\nDeploy: copy data/google-workspace-creds/ to server and restart the bot.")


def _save_creds(path: Path, creds):
    """Save credentials in workspace-mcp JSON format."""
    creds_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else SCOPES,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }
    with open(path, "w") as f:
        json.dump(creds_data, f, indent=2)


if __name__ == "__main__":
    main()
