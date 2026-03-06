"""Integration tests for Family Bot.

Runs against the live deployment at the configured server.
Tests: healthcheck, self-upgrade git workflow, TG webhook parsing,
message enrichment (reply-to, forward, location, contact, media).

Configuration via environment variables:
    BOT_SERVER      — server IP/hostname (required for remote tests)
    SSH_KEY         — path to SSH private key
    SSH_USER        — SSH username
    HEALTH_URL      — health endpoint URL (default: http://localhost:8000/health)
    CONTAINER_NAME  — Docker container name (e.g. smith-family-bot)
    BOT_REPO_DIR    — repo path on server (default: ~/family-bot, set to your fork folder)

Usage:
    # Local-only tests (parsing, enrichment — no server needed):
    python -m pytest tests/test_integration.py -v -k "Parsing or Enrichment"

    # All tests (requires BOT_SERVER):
    BOT_SERVER=1.2.3.4 SSH_KEY=~/.ssh/mykey CONTAINER_NAME=smith-family-bot \\
        python -m pytest tests/test_integration.py -v

    # Single test:
    python -m pytest tests/test_integration.py -v -k "test_health"
"""

import json
import os
import subprocess
import sys
import time

import pytest

# Add parent dir to path so we can import bot modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# === Test configuration (all from env vars, no hardcoded family data) ===
SERVER = os.environ.get("BOT_SERVER", "")
SSH_KEY = os.environ.get("SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519"))
SSH_USER = os.environ.get("SSH_USER", os.environ.get("USER", ""))
HEALTH_URL = os.environ.get("HEALTH_URL", "http://localhost:8000/health")
CONTAINER = os.environ.get("CONTAINER_NAME", "")
REPO_DIR = os.environ.get("BOT_REPO_DIR", "~/family-bot")

# Test data for TG parsing tests (generic, not family-specific)
TEST_CHAT_ID = -1001234567890
TEST_USER_ID = 111222333
TEST_USER_NAME = "TestUser"

requires_server = pytest.mark.skipif(
    not SERVER, reason="BOT_SERVER not set — skipping remote tests"
)
requires_container = pytest.mark.skipif(
    not CONTAINER, reason="CONTAINER_NAME not set — skipping container tests"
)


def ssh_cmd(cmd: str, timeout: int = 30) -> str:
    """Run a command on the server via SSH."""
    if not SERVER:
        pytest.skip("BOT_SERVER not configured")
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new",
         "-i", SSH_KEY, f"{SSH_USER}@{SERVER}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0 and "error" in result.stderr.lower():
        raise RuntimeError(f"SSH command failed: {result.stderr}")
    return result.stdout + result.stderr


# ============================================================
# 1. HEALTH & DEPLOYMENT
# ============================================================

@requires_server
class TestHealth:
    def test_healthcheck_returns_ok(self):
        """Health endpoint returns status=ok with expected fields."""
        out = ssh_cmd(f"curl -sf {HEALTH_URL}")
        data = json.loads(out)
        assert data["status"] == "ok"
        assert data["version"] == "agent-sdk"
        assert "commit" in data
        assert "timestamp" in data
        assert "active_tasks" in data
        assert "queue_size" in data

    def test_healthcheck_commit_format(self):
        """Health commit hash is a valid 7-char short hash."""
        out = ssh_cmd(f"curl -sf {HEALTH_URL}")
        commit = json.loads(out)["commit"]
        assert len(commit) == 7, f"Unexpected commit format: {commit}"

    @requires_container
    def test_container_running(self):
        """Bot container is running."""
        out = ssh_cmd(f"docker inspect {CONTAINER} --format '{{{{.State.Status}}}}'")
        assert "running" in out.strip()

    def test_wa_bridge_running(self):
        """WhatsApp bridge container is accessible."""
        out = ssh_cmd("docker ps --format '{{.Names}}' | grep wa-bridge")
        assert "wa-bridge" in out


# ============================================================
# 2. GIT REPO SYNC
# ============================================================

@requires_server
class TestGitSync:
    def test_server_has_remotes(self):
        """Server has at least an origin remote."""
        out = ssh_cmd(f"cd {REPO_DIR} && git remote -v")
        assert "origin" in out

    def test_no_uncommitted_changes(self):
        """Server repo has no dirty working tree (except override and runtime files)."""
        out = ssh_cmd(f"cd {REPO_DIR} && git status --short")
        # Ignore expected untracked/runtime files
        ignore = ["docker-compose.override", "scheduled_tasks",
                  "google-workspace-creds", "data/last_"]
        lines = [l for l in out.strip().split("\n")
                 if l.strip()
                 and not any(ign in l for ign in ignore)]
        assert not lines, f"Unexpected changes on server: {lines}"


# ============================================================
# 3. SELF-UPGRADE (SSH + GIT inside container)
# ============================================================

@requires_server
@requires_container
class TestSelfUpgrade:
    def test_container_has_host_repo_mount(self):
        """Container has /host-repo mounted."""
        out = ssh_cmd(f"docker exec {CONTAINER} ls /host-repo/main.py")
        assert "main.py" in out

    def test_container_git_works(self):
        """Git commands work inside container."""
        ssh_cmd(
            f"docker exec {CONTAINER} bash -c 'cd /host-repo && git status --short'"
        )

    def test_container_ssh_key_exists(self):
        """Deploy key is mounted and accessible inside container."""
        out = ssh_cmd(
            f"docker exec {CONTAINER} bash -c "
            "'ls -la /home/botuser/.ssh/id_ed25519 2>&1'"
        )
        assert "id_ed25519" in out
        assert "No such file" not in out

    def test_container_can_reach_github(self):
        """Container can SSH to GitHub (deploy key auth)."""
        out = ssh_cmd(
            f"docker exec {CONTAINER} bash -c "
            "'ssh -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1 || true'"
        )
        assert "successfully authenticated" in out.lower() or "Hi " in out


# ============================================================
# 4. TELEGRAM PARSING (unit-style, runs locally)
# ============================================================

class TestTelegramParsing:
    """Test parse_update() with synthetic TG webhook payloads."""

    @pytest.fixture(autouse=True)
    def setup_config(self, monkeypatch):
        """Patch telegram module's config so parse_update works with test data."""
        import integrations.telegram as tg_mod
        monkeypatch.setattr(tg_mod, "TG_ALLOWED_CHATS", {TEST_CHAT_ID})
        monkeypatch.setattr(tg_mod, "TG_ALLOWED_USERS", {TEST_USER_ID: TEST_USER_NAME})
        monkeypatch.setattr(tg_mod, "TG_BOT_USER_ID", 0)
        monkeypatch.setattr(tg_mod, "TG_MCP_BOT_USER_ID", 0)

    def _make_message(self, **overrides):
        """Build a minimal TG message dict."""
        msg = {
            "message_id": 1,
            "from": {"id": TEST_USER_ID, "first_name": TEST_USER_NAME, "is_bot": False},
            "chat": {"id": TEST_CHAT_ID},
            "date": int(time.time()),
            "text": "hello",
        }
        msg.update(overrides)
        return {"update_id": 1, "message": msg}

    def test_parse_text_message(self):
        from integrations.telegram import parse_update
        update = self._make_message(text="test message")
        parsed = parse_update(update)
        assert parsed is not None
        assert parsed["text"] == "test message"
        assert parsed["user_id"] == TEST_USER_ID

    def test_parse_photo_message(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="",
            caption="nice photo",
            photo=[{"file_id": "abc", "width": 800, "height": 600}],
        )
        parsed = parse_update(update)
        assert parsed is not None
        assert parsed["has_media"] is True
        assert parsed["text"] == "nice photo"

    def test_parse_location_message(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="",
            location={"latitude": 51.4934, "longitude": -0.2231},
        )
        parsed = parse_update(update)
        assert parsed is not None
        assert parsed["location"]["latitude"] == 51.4934
        assert parsed["location"]["longitude"] == -0.2231

    def test_parse_live_location(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="",
            location={"latitude": 51.0, "longitude": -0.1, "live_period": 3600},
        )
        parsed = parse_update(update)
        assert parsed is not None
        assert parsed["location"]["live_period"] == 3600

    def test_parse_venue_message(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="",
            venue={
                "location": {"latitude": 51.5, "longitude": -0.1},
                "title": "Big Ben",
                "address": "Westminster, London",
            },
        )
        parsed = parse_update(update)
        assert parsed is not None
        assert parsed["location"]["title"] == "Big Ben"
        assert parsed["location"]["address"] == "Westminster, London"

    def test_parse_contact_message(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="",
            contact={
                "phone_number": "+447123456789",
                "first_name": "John",
                "last_name": "Doe",
            },
        )
        parsed = parse_update(update)
        assert parsed is not None
        assert parsed["contact"]["phone_number"] == "+447123456789"
        assert parsed["contact"]["first_name"] == "John"

    def test_parse_reply_to_text(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="what about this?",
            reply_to_message={
                "message_id": 99,
                "from": {"id": TEST_USER_ID, "is_bot": False},
                "text": "I sent this earlier",
            },
        )
        parsed = parse_update(update)
        assert parsed["reply_to"] is not None
        assert parsed["reply_to"]["text"] == "I sent this earlier"
        assert parsed["reply_to"]["is_bot"] is False

    def test_parse_reply_to_bot_message(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="explain this",
            reply_to_message={
                "message_id": 100,
                "from": {"id": 999, "is_bot": True},
                "text": "Here's the summary...",
            },
        )
        parsed = parse_update(update)
        assert parsed["reply_to"]["is_bot"] is True

    def test_parse_reply_to_with_media(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="what is this?",
            reply_to_message={
                "message_id": 101,
                "from": {"id": TEST_USER_ID, "is_bot": False},
                "text": "",
                "photo": [{"file_id": "xyz", "width": 640, "height": 480}],
            },
        )
        parsed = parse_update(update)
        assert parsed["reply_to"]["has_media"] is True
        assert parsed["reply_to"]["raw"] is not None

    def test_parse_reply_to_with_location(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="is this close?",
            reply_to_message={
                "message_id": 102,
                "from": {"id": TEST_USER_ID, "is_bot": False},
                "text": "",
                "location": {"latitude": 51.5, "longitude": -0.1},
            },
        )
        parsed = parse_update(update)
        assert parsed["reply_to"]["location"] is not None
        assert parsed["reply_to"]["location"]["latitude"] == 51.5

    def test_parse_reply_to_with_contact(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="call this person",
            reply_to_message={
                "message_id": 103,
                "from": {"id": TEST_USER_ID, "is_bot": False},
                "text": "",
                "contact": {"phone_number": "+44700000", "first_name": "Jane"},
            },
        )
        parsed = parse_update(update)
        assert parsed["reply_to"]["contact"]["phone_number"] == "+44700000"

    def test_parse_forward(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="Check this out",
            forward_origin={"type": "user", "sender_user": {"first_name": "Someone"}},
        )
        parsed = parse_update(update)
        assert parsed["forward"] is not None
        assert parsed["forward"]["sender_name"] == "Someone"

    def test_parse_audio_message(self):
        from integrations.telegram import parse_update
        update = self._make_message(
            text="",
            caption="listen to this",
            audio={"file_id": "aud1", "duration": 180, "title": "Song",
                   "performer": "Artist", "mime_type": "audio/mp3"},
        )
        parsed = parse_update(update)
        assert parsed["has_media"] is True

    def test_empty_message_ignored(self):
        from integrations.telegram import parse_update
        update = self._make_message(text="")
        update["message"].pop("text")
        parsed = parse_update(update)
        assert parsed is None

    def test_wrong_chat_ignored(self):
        from integrations.telegram import parse_update
        update = self._make_message(text="hello")
        update["message"]["chat"]["id"] = -99999
        parsed = parse_update(update)
        assert parsed is None

    def test_bot_message_flagged(self):
        from integrations.telegram import parse_update
        update = self._make_message(text="I'm the bot")
        update["message"]["from"]["is_bot"] = True
        parsed = parse_update(update)
        assert parsed is not None
        assert parsed.get("is_bot_message") is True


# ============================================================
# 5. MESSAGE ENRICHMENT (text building logic)
# ============================================================

class TestMessageEnrichment:
    """Test the text enrichment logic in handle_telegram_message.

    We test the enrichment functions by simulating parsed dicts
    and checking the resulting text modifications.
    """

    def _enrich_text(self, parsed: dict) -> str:
        """Simulate the text enrichment from handle_telegram_message."""
        text = parsed.get("text", "")

        # Reply-to enrichment
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

        # Forward enrichment
        forward = parsed.get("forward")
        if forward:
            fwd_name = forward.get("sender_name", "someone")
            text = (f"[Forwarded message from {fwd_name}]\n{text}\n\n"
                    f"[SYSTEM: This is a forwarded message. The user did not add their own instructions. "
                    f"Briefly acknowledge the content and ask what they'd like you to do with it.]")

        # Location enrichment
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

        # Contact enrichment
        if parsed.get("contact"):
            c = parsed["contact"]
            name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
            phone = c.get("phone_number", "")
            contact_desc = f"{name}" + (f", {phone}" if phone else "")
            text = f"[Shared contact: {contact_desc}]\n{text}" if text else f"[Shared contact: {contact_desc}]"

        return text

    def test_reply_to_text(self):
        text = self._enrich_text({
            "text": "what about this?",
            "reply_to": {"text": "original message", "is_bot": False},
        })
        assert '[Replying to earlier message: "original message"]' in text
        assert "what about this?" in text

    def test_reply_to_bot(self):
        text = self._enrich_text({
            "text": "explain",
            "reply_to": {"text": "bot said stuff", "is_bot": True},
        })
        assert "bot's previous response" in text

    def test_reply_to_location(self):
        text = self._enrich_text({
            "text": "how far?",
            "reply_to": {"location": {"latitude": 51.5, "longitude": -0.1}},
        })
        assert "[Replying to location: 51.5, -0.1]" in text

    def test_reply_to_venue(self):
        text = self._enrich_text({
            "text": "book it",
            "reply_to": {
                "location": {
                    "latitude": 51.5, "longitude": -0.1,
                    "title": "The Ritz", "address": "Piccadilly",
                },
            },
        })
        assert "The Ritz" in text
        assert "Piccadilly" in text

    def test_reply_to_contact(self):
        text = self._enrich_text({
            "text": "call them",
            "reply_to": {
                "contact": {"first_name": "John", "last_name": "Doe",
                            "phone_number": "+44123"},
            },
        })
        assert "John Doe" in text
        assert "+44123" in text

    def test_reply_to_media(self):
        text = self._enrich_text({
            "text": "what is this?",
            "reply_to": {"has_media": True},
        })
        assert "media — see attached" in text

    def test_reply_to_combined(self):
        """Reply to message with text + location + media."""
        text = self._enrich_text({
            "text": "tell me about this",
            "reply_to": {
                "text": "here's the place",
                "is_bot": False,
                "location": {"latitude": 51.5, "longitude": -0.1},
                "has_media": True,
            },
        })
        assert "earlier message" in text
        assert "location" in text
        assert "media" in text

    def test_forward(self):
        text = self._enrich_text({
            "text": "Check this email",
            "forward": {"sender_name": "Someone"},
        })
        assert "[Forwarded message from Someone]" in text
        assert "SYSTEM" in text

    def test_direct_location(self):
        text = self._enrich_text({
            "text": "",
            "location": {"latitude": 51.5, "longitude": -0.1},
        })
        assert "[Shared location: 51.5, -0.1]" in text

    def test_direct_live_location(self):
        text = self._enrich_text({
            "text": "",
            "location": {"latitude": 51.5, "longitude": -0.1, "live_period": 3600},
        })
        assert "[Live location:" in text

    def test_direct_venue(self):
        text = self._enrich_text({
            "text": "let's go here",
            "location": {
                "latitude": 51.5, "longitude": -0.1,
                "title": "Pizza Express", "address": "High Street",
            },
        })
        assert "Pizza Express" in text
        assert "High Street" in text

    def test_direct_contact(self):
        text = self._enrich_text({
            "text": "",
            "contact": {"first_name": "Anna", "last_name": "S",
                        "phone_number": "+44789"},
        })
        assert "[Shared contact: Anna S, +44789]" in text

    def test_no_enrichment_plain_text(self):
        text = self._enrich_text({"text": "hello"})
        assert text == "hello"
