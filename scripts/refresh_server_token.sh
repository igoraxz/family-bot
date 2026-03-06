#!/bin/bash
# Refresh the Claude Code OAuth token on the server from macOS Keychain.
# Run locally on macOS: ./scripts/refresh_server_token.sh
#
# Writes the full credentials JSON (with refresh token) to the container's
# ~/.claude/.credentials.json so the CLI subprocess can auto-refresh.
# Does NOT set CLAUDE_CODE_OAUTH_TOKEN env var (that blocks auto-refresh).

set -euo pipefail

SERVER="${SERVER:-user@your-server-ip}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
CONTAINER="${CONTAINER:-family-bot}"
REMOTE_DIR="${REMOTE_DIR:-~/family-bot}"
KEYCHAIN_ACCOUNT="${KEYCHAIN_ACCOUNT:-$(whoami)}"

echo "Extracting token from macOS Keychain..."
CREDS_JSON=$(security find-generic-password -s "Claude Code-credentials" -a "$KEYCHAIN_ACCOUNT" -w 2>/dev/null)
if [ -z "$CREDS_JSON" ]; then
    echo "ERROR: No Claude Code credentials found in Keychain."
    echo "Make sure you're logged in: claude auth login"
    exit 1
fi

EXPIRES_IN=$(echo "$CREDS_JSON" | python3 -c "
import sys,json,time
exp = json.load(sys.stdin)['claudeAiOauth']['expiresAt'] / 1000
remaining = exp - time.time()
print(f'{remaining/3600:.1f}h')
")

echo "Token valid for: $EXPIRES_IN"

# Write full credentials JSON to container's CLI storage path
# Claude CLI reads from ~/.claude/.credentials.json (note the dot prefix!)
echo "Writing credentials to container..."
echo "$CREDS_JSON" | ssh -i "$SSH_KEY" "$SERVER" "docker exec -i $CONTAINER tee /home/botuser/.claude/.credentials.json > /dev/null"
ssh -i "$SSH_KEY" "$SERVER" "docker exec $CONTAINER chown botuser:botuser /home/botuser/.claude/.credentials.json"
ssh -i "$SSH_KEY" "$SERVER" "docker exec $CONTAINER chmod 600 /home/botuser/.claude/.credentials.json"

# Remove CLAUDE_CODE_OAUTH_TOKEN from .env if present (it overrides the file
# and blocks auto-refresh because the CLI sets refreshToken=null for env tokens)
echo "Removing CLAUDE_CODE_OAUTH_TOKEN from .env (file-based auth is better)..."
ssh -i "$SSH_KEY" "$SERVER" "sed -i '/^CLAUDE_CODE_OAUTH_TOKEN=/d' $REMOTE_DIR/.env"

echo "Restarting container to pick up new credentials..."
ssh -i "$SSH_KEY" "$SERVER" "cd $REMOTE_DIR && docker compose restart bot-core"

echo "Done! Credentials written with refresh token — CLI will auto-refresh."
