"""Flexible scheduled tasks system.

Tasks are stored in data/scheduled_tasks.json and can be managed
conversationally via MCP tools. The scheduler loop in main.py calls
check_custom_tasks() every 30s to fire due tasks.

Each task has:
  - id: unique string (auto-generated or fixed for defaults)
  - name: human-readable name
  - hour: 0-23 (for fixed-time tasks)
  - minute: 0-59
  - days: list of weekday names ["mon","tue",...] or ["daily"]
  - prompt: the system prompt to execute when task fires
  - platform: "telegram" | "whatsapp" | "both" (where to send output)
  - enabled: bool
  - last_run: ISO date string (YYYY-MM-DD) or ISO datetime for interval tasks
  - created_at: ISO datetime
  - interval_hours: (optional) float — if set, task fires every N hours instead of at a fixed time.
    The hour/minute fields are ignored for interval tasks; only last_run matters.
"""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DATA_DIR, FAMILY_TIMEZONE

log = logging.getLogger(__name__)

TASKS_FILE = DATA_DIR / "scheduled_tasks.json"
TZ = ZoneInfo(FAMILY_TIMEZONE)

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_MAP = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed",
    "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun",
    "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
    "fri": "fri", "sat": "sat", "sun": "sun",
    "daily": "daily", "weekdays": "weekdays", "weekends": "weekends",
}


def _load_tasks() -> list[dict]:
    """Load tasks from JSON file."""
    if not TASKS_FILE.exists():
        return []
    try:
        data = json.loads(TASKS_FILE.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Failed to load scheduled tasks: {e}")
        return []


def _save_tasks(tasks: list[dict]) -> None:
    """Save tasks to JSON file."""
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(tasks, indent=2, ensure_ascii=False))


def _normalize_days(days_input: list[str]) -> list[str]:
    """Normalize day names to short form. Expand 'daily', 'weekdays', 'weekends'."""
    result = []
    for d in days_input:
        d_lower = d.lower().strip()
        mapped = DAY_MAP.get(d_lower)
        if mapped == "daily":
            return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        elif mapped == "weekdays":
            result.extend(["mon", "tue", "wed", "thu", "fri"])
        elif mapped == "weekends":
            result.extend(["sat", "sun"])
        elif mapped:
            result.append(mapped)
        else:
            log.warning(f"Unknown day name: {d}")
    return list(set(result)) if result else ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def list_tasks() -> list[dict]:
    """Return all scheduled tasks."""
    return _load_tasks()


def get_task(task_id: str) -> dict | None:
    """Get a single task by ID."""
    for t in _load_tasks():
        if t["id"] == task_id:
            return t
    return None


def add_task(
    name: str,
    hour: int,
    minute: int,
    prompt: str,
    days: list[str] | None = None,
    platform: str = "telegram",
    enabled: bool = True,
) -> dict:
    """Add a new scheduled task. Returns the created task."""
    tasks = _load_tasks()
    task = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "hour": hour,
        "minute": minute,
        "days": _normalize_days(days or ["daily"]),
        "prompt": prompt,
        "platform": platform,
        "enabled": enabled,
        "last_run": "",
        "created_at": datetime.now(TZ).isoformat(),
    }
    tasks.append(task)
    _save_tasks(tasks)
    log.info(f"Scheduled task added: {task['name']} ({task['id']})")
    return task


def update_task(task_id: str, **kwargs) -> dict | None:
    """Update fields of an existing task. Returns updated task or None."""
    tasks = _load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            for key, val in kwargs.items():
                if key == "days" and val is not None:
                    val = _normalize_days(val)
                if key in t:
                    t[key] = val
            _save_tasks(tasks)
            log.info(f"Scheduled task updated: {t['name']} ({task_id})")
            return t
    return None


def delete_task(task_id: str) -> bool:
    """Delete a task by ID. Returns True if found and deleted."""
    tasks = _load_tasks()
    original_len = len(tasks)
    tasks = [t for t in tasks if t["id"] != task_id]
    if len(tasks) < original_len:
        _save_tasks(tasks)
        log.info(f"Scheduled task deleted: {task_id}")
        return True
    return False


def toggle_task(task_id: str) -> dict | None:
    """Toggle enabled/disabled. Returns updated task."""
    tasks = _load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["enabled"] = not t["enabled"]
            _save_tasks(tasks)
            log.info(f"Task {task_id} toggled to {'enabled' if t['enabled'] else 'disabled'}")
            return t
    return None


def get_due_tasks() -> list[dict]:
    """Return tasks that are due right now.

    Fixed-time tasks: match hour, minute, day, not yet run today.
    Interval tasks: fire every interval_hours since last_run.
    """
    now = datetime.now(TZ)
    today_str = str(now.date())
    current_day = DAY_NAMES[now.weekday()]  # 0=Monday

    due = []
    for task in _load_tasks():
        if not task.get("enabled", True):
            continue

        interval = task.get("interval_hours")
        if interval:
            # Interval-based task
            last_run_str = task.get("last_run", "")
            if last_run_str:
                try:
                    last_run = datetime.fromisoformat(last_run_str)
                    if last_run.tzinfo is None:
                        last_run = last_run.replace(tzinfo=TZ)
                    hours_since = (now - last_run).total_seconds() / 3600
                    if hours_since < interval:
                        continue
                except (ValueError, TypeError):
                    pass  # Invalid last_run — fire it
            due.append(task)
        else:
            # Fixed-time task
            if task.get("last_run", "").startswith(today_str):
                continue
            if task["hour"] != now.hour:
                continue
            if now.minute < task["minute"]:
                continue
            task_days = task.get("days", DAY_NAMES)
            if current_day not in task_days:
                continue
            due.append(task)

    return due


def mark_task_run(task_id: str) -> None:
    """Mark a task as having run. Uses ISO datetime for interval tasks, date for fixed-time."""
    tasks = _load_tasks()
    now = datetime.now(TZ)
    for t in tasks:
        if t["id"] == task_id:
            if t.get("interval_hours"):
                t["last_run"] = now.isoformat()
            else:
                t["last_run"] = str(now.date())
            break
    _save_tasks(tasks)


def format_task_list(tasks: list[dict]) -> str:
    """Format tasks into a readable string for display."""
    if not tasks:
        return "No scheduled tasks configured."

    lines = []
    for t in tasks:
        status = "ON" if t.get("enabled", True) else "OFF"
        interval = t.get("interval_hours")

        if interval:
            time_str = f"every {interval}h"
        else:
            days = t.get("days", ["daily"])
            if set(days) == set(DAY_NAMES):
                days_str = "daily"
            elif set(days) == {"mon", "tue", "wed", "thu", "fri"}:
                days_str = "weekdays"
            elif set(days) == {"sat", "sun"}:
                days_str = "weekends"
            else:
                days_str = ", ".join(sorted(days, key=lambda d: DAY_NAMES.index(d)))
            time_str = f"{t['hour']:02d}:{t['minute']:02d} {days_str}"

        lines.append(
            f"[{status}] {t['name']} - {time_str} "
            f"({t['platform']}) [id: {t['id']}]"
        )
    return "\n".join(lines)


# === DEFAULT TASKS ===

DEFAULT_TASKS = [
    {
        "id": "morning",
        "name": "Morning Briefing",
        "hour": 7,
        "minute": 10,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "prompt": (
            "[DAILY_PROACTIVE] Send your daily proactive summary.\n\n"
            "Check calendars (get_events), recent emails (search_gmail_messages for last 12h), "
            "and knowledge base.\n"
            "Include: today's schedule, reminders, and 1-2 suggestions.\n"
            "Morning reminder — be warm and concise.\n\n"
            "Send to BOTH channels:\n"
            "1. Telegram: use telegram_send_message (HTML). Tag both parents.\n"
            "2. WhatsApp: use send_message (plain text, no HTML)."
        ),
        "platform": "both",
        "enabled": True,
    },
    {
        "id": "email_check",
        "name": "Email Monitor",
        "hour": 9,
        "minute": 0,
        "interval_hours": 3,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "prompt": (
            "[EMAIL_CHECK] Periodic email monitoring.\n\n"
            "1. Search recent emails (search_gmail_messages for last 3 hours).\n"
            "2. Look for urgent/time-sensitive items: school notices, appointments, deliveries.\n"
            "3. If anything URGENT: send alert via telegram_send_message.\n"
            "4. If nothing urgent: just return a brief log summary (don't send to chat).\n"
            "5. Update knowledge base with relevant info."
        ),
        "platform": "telegram",
        "enabled": True,
    },
    {
        "id": "evening1",
        "name": "Evening Prep Reminder",
        "hour": 18,
        "minute": 0,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "prompt": (
            "[EVENING_PREP] It's 6pm. Check tomorrow's calendar events and recent emails "
            "for anything that needs preparation tonight or early tomorrow.\n\n"
            "Look for:\n"
            "1. Tomorrow's calendar events (school, appointments, activities)\n"
            "2. Any items needing prep (packed lunches, PE kit, costumes, forms to sign)\n"
            "3. Any deadlines or payments due tomorrow\n"
            "4. Weather forecast if relevant to plans\n\n"
            "Send to BOTH channels:\n"
            "1. Telegram: use telegram_send_message (HTML). Tag both parents.\n"
            "2. WhatsApp: use send_message (plain text, no HTML).\n\n"
            "Format: brief bullet list of what's happening tomorrow and what to prep tonight.\n"
            "Keep it warm and helpful, not overwhelming."
        ),
        "platform": "both",
        "enabled": True,
    },
    {
        "id": "daily_selfopt",
        "name": "Daily Self-Optimization Review",
        "hour": 9,
        "minute": 0,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "prompt": (
            "[SELF_OPTIMIZATION] Daily bot health, performance, and observability review.\n\n"
            "SCOPE: Check ALL monitored parameters and system health — not just RAG.\n\n"
            "1. Check recent logs for errors and warnings (use Bash: tail -200 on log files).\n"
            "2. Run rag_stats to check RAG index health (chunk count, embedding failures).\n"
            "3. Review any failed tool calls or recurring issues.\n"
            "4. Analyze RAG search quality: check similarity score distribution if possible.\n"
            "5. Consider parameter tuning: chunk size (7), stride (3), threshold (0.25).\n"
            "6. Check scheduler task execution: any tasks failing or timing out?\n"
            "7. Review memory system: DB size, knowledge base growth, facts count.\n"
            "8. Check tool response times: any tools consistently slow?\n"
            "9. Review any NEW parameters or features added since last check.\n"
            "   (Read recent git log to see what changed: Bash 'cd /host-repo && git log --oneline -10')\n"
            "10. For any new feature/parameter found in recent commits:\n"
            "    - Verify it has logging/observability\n"
            "    - Check if it's working as expected from logs\n"
            "    - Note if monitoring needs to be added\n\n"
            "IMPORTANT: If there are NO meaningful findings — no errors, no warnings, "
            "no performance concerns — stay COMPLETELY SILENT. Do not send any message.\n\n"
            "Only if you find actionable issues or concrete tuning recommendations "
            "with supporting data:\n"
            "- Send findings + recommendations via telegram_send_message to the admin.\n"
            "- Request admin approval before making any changes.\n"
            "- Include specific metrics and evidence for each recommendation."
        ),
        "platform": "telegram",
        "enabled": True,
    },
]


def init_default_tasks() -> None:
    """Initialize default tasks if the tasks file is empty or missing.

    Only adds defaults whose IDs don't already exist (preserves user edits).
    """
    tasks = _load_tasks()
    existing_ids = {t["id"] for t in tasks}
    added = 0

    for default in DEFAULT_TASKS:
        if default["id"] not in existing_ids:
            task = {
                **default,
                "last_run": "",
                "created_at": datetime.now(TZ).isoformat(),
            }
            tasks.append(task)
            added += 1
            log.info(f"Default task added: {task['name']} ({task['id']})")

    if added:
        _save_tasks(tasks)
        log.info(f"Initialized {added} default scheduled task(s)")
