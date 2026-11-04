"""Outbound notifications: a POST to each webhook in config/notifications.yaml
when the account hits its Claude usage limit (``credit_limit``) and when that
hold lifts (``credit_restored``). No model is involved — the runner itself
detects both and posts.

It runs inside the runner's tick, so it must never raise and never block for
long: a 5 s timeout per request, at most three attempts, failures only logged.
The webhook URL is a secret (anyone holding it can post to the channel) and is
never logged — only its host.

The body is shaped for a Discord webhook (``content``, ``username``, one embed
whose fields carry the details) and also carries the same details as top-level
keys for any other receiver. If a receiver rejects the extra keys (HTTP 400),
the Discord-only body is sent instead.

config/notifications.yaml (written by the hosting platform from the account's
Notifications setting; hand-written on a self-hosted runner)::

    webhooks:
      - url: https://discord.com/api/webhooks/...
        events: [credit_limit, credit_restored]   # optional; default = all events

See docs/notifications.md.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

import credit_hold

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_NAME = "notifications.yaml"

EVENTS = ("credit_limit", "credit_restored")
USERNAME = "AI Task Runner"
TIMEOUT_S = 5
MAX_ATTEMPTS = 3  # covers one Discord-shape fallback and one retry

_COLOUR = {"credit_limit": 0xE67E22, "credit_restored": 0x2ECC71}


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] notify: {msg}", flush=True)


def account_name(server_url: str | None = None) -> str:
    """The runner account this container serves: the first label of SERVER_URL's
    host (``moment`` for https://moment.ai-task-runner.com). The runner cannot
    know which Claude account sits behind its token, so this is the name the
    message carries."""
    url = server_url if server_url is not None else os.environ.get("SERVER_URL", "")
    host = urlparse(url).hostname or ""
    return host.split(".")[0] if host else "this runner"


def load_webhooks(config_dir: Path | None = None) -> list[dict]:
    """[{url, events}] from notifications.yaml; [] when absent or unusable.
    Entries must be https; a bare string is a URL subscribed to every event."""
    path = Path(config_dir or CONFIG_DIR) / CONFIG_NAME
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as e:  # noqa: BLE001 — a bad file must not break the tick
        log(f"{CONFIG_NAME} unreadable ({e}); no notifications")
        return []
    hooks = []
    for entry in (data.get("webhooks") or []) if isinstance(data, dict) else []:
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url.startswith("https://"):
            continue
        events = entry.get("events")
        if events is None:
            events = list(EVENTS)
        if not isinstance(events, list):
            continue
        hooks.append({"url": url, "events": [str(e).strip() for e in events]})
    return hooks


def when(reset_at: str, now: datetime | None = None) -> str:
    """'2026-11-04 17:00 UTC (in 2h 10m)' for an ISO reset time."""
    dt = credit_hold.parse_iso(reset_at)
    if dt is None:
        return reset_at or "unknown"
    now = now or datetime.now(timezone.utc)
    mins = int((dt - now).total_seconds() // 60)
    if mins <= 0:
        rel = "now"
    elif mins < 60:
        rel = f"in {mins}m"
    else:
        rel = f"in {mins // 60}h {mins % 60:02d}m"
    return f"{dt.strftime('%Y-%m-%d %H:%M UTC')} ({rel})"


def build_content(event: str, fields: dict, now: datetime | None = None) -> str:
    """The one human line a chat channel shows."""
    account = fields.get("account") or account_name()
    kind = fields.get("limit_kind") or "usage"
    if event == "credit_limit":
        parked = int(fields.get("parked_tasks") or 1)
        text = (f"⚠️ Task Runner **{account}** hit its Claude {kind} limit — "
                f"{parked} task{'' if parked == 1 else 's'} parked, "
                f"resumes {when(str(fields.get('reset_at') or ''), now)}.")
        project, task_id = fields.get("project"), fields.get("task_id")
        if project or task_id:
            text += f" Last task: {project or '?'} / {task_id or '?'}."
        return text
    if event == "credit_restored":
        return (f"✅ Task Runner **{account}** is running again — "
                f"the Claude {kind} limit has lifted.")
    return f"ℹ️ Task Runner **{account}**: {event}."


def build_payload(event: str, fields: dict, now: datetime | None = None) -> dict:
    """Discord-shaped body plus the same details as top-level keys."""
    now = now or datetime.now(timezone.utc)
    details = {
        "event": event,
        "account": fields.get("account") or account_name(),
        "limit_kind": str(fields.get("limit_kind") or ""),
        "reset_at": str(fields.get("reset_at") or ""),
        "parked_tasks": int(fields.get("parked_tasks") or 0),
        "project": str(fields.get("project") or ""),
        "task_id": str(fields.get("task_id") or ""),
        "sent_at": now.isoformat(),
    }
    content = build_content(event, details, now)
    embed_fields = [{"name": k.replace("_", " "), "value": str(v) or "—", "inline": True}
                    for k, v in details.items() if k not in ("event", "sent_at") and v not in ("", 0)]
    payload = {
        "content": content,
        "username": USERNAME,
        "embeds": [{"title": event.replace("_", " "),
                    "color": _COLOUR.get(event, 0x95A5A6),
                    "fields": embed_fields}],
    }
    payload.update(details)
    return payload


def discord_only(payload: dict) -> dict:
    return {k: payload[k] for k in ("content", "username", "embeds") if k in payload}


def _post(url: str, body: dict) -> int:
    """POST the body as JSON; the HTTP status (4xx/5xx included). Transport
    errors propagate to the caller."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "ai-task-runner-notify"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as e:
        return int(e.code)


def _deliver(url: str, payload: dict) -> bool:
    host = urlparse(url).hostname or "webhook"
    body = payload
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            status = _post(url, body)
        except Exception as e:  # noqa: BLE001 — transport trouble: retry, then give up
            log(f"{host}: attempt {attempt} failed ({e.__class__.__name__}: {e})")
            continue
        if 200 <= status < 300:
            return True
        if status == 400 and body is payload:
            log(f"{host} rejected the body (HTTP 400); resending the Discord-only shape")
            body = discord_only(payload)
            continue
        log(f"{host} returned HTTP {status}")
        if status < 500:
            return False  # a 4xx other than the shape fallback will not improve on retry
    return False


def send(event: str, config_dir: Path | None = None, **fields) -> int:
    """Notify every webhook subscribed to `event`. Returns how many accepted it.
    Never raises — a notification must never be the reason a task fails."""
    try:
        hooks = [h for h in load_webhooks(config_dir) if event in h["events"]]
        if not hooks:
            return 0
        payload = build_payload(event, fields)
        delivered = sum(1 for h in hooks if _deliver(h["url"], payload))
        log(f"{event}: {delivered}/{len(hooks)} webhook(s) notified")
        return delivered
    except Exception as e:  # noqa: BLE001
        log(f"{event} failed (continuing): {e}")
        return 0
