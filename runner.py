#!/usr/bin/env python3
"""
Claude Task Runner — Task Processor

Called every 5 seconds by the entrypoint loop. Scans the queue, claims the oldest task via
atomic rename, and executes it via headless Claude Code CLI (claude -p).

Concurrency: multiple runner processes can coexist safely. Atomic os.rename()
prevents double-claiming. MAX_CONCURRENT limits how many tasks run at once.

Usage:
    python runner.py                    # Process one task (called by entrypoint loop)
    MAX_CONCURRENT=4 python runner.py   # Override concurrency limit
"""

import hashlib
import json as json_mod
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import frontmatter
import yaml

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
TASKS_DIR = BASE_DIR / "tasks"
LOGS_DIR = BASE_DIR / "logs"

# Per-project requirements manifest (in each project repo root) and the
# directory where the hosting platform materializes per-project MCP config
# (bind-mounted read-only into the container; absent on self-hosted setups).
MANIFEST_NAME = "agent-requirements.yaml"
MCP_CONFIG_DIR = Path(os.environ.get("MCP_CONFIG_DIR", "/secrets/mcp"))

QUEUED_DIR = TASKS_DIR / "queued"
PROCESSING_DIR = TASKS_DIR / "processing"
DONE_DIR = TASKS_DIR / "done"
FAILED_DIR = TASKS_DIR / "failed"
AWAITING_CREDITS_DIR = TASKS_DIR / "awaiting_credits"
AWAITING_WEEKLY_RESET_DIR = TASKS_DIR / "awaiting_weekly_reset"
# Non-secret fingerprint of the Claude account whose credit limit last parked a
# task. Stored under tasks/ (rw) because config/ is mounted read-only. When the
# connected token changes, any credit hold parked under the old account is stale
# (a new account has its own credit pool), so it gets cleared — see
# clear_credit_hold_on_token_change().
AUTH_FP_FILE = TASKS_DIR / ".claude_auth_fp"

# Pattern for Claude's credit-limit error, e.g.
#   "You've hit your limit · resets 9:30pm (UTC)"     (kind absent — current format)
#   "You've hit your session limit · resets 3:45pm"
#   "You've hit your weekly limit · resets Mon 12:00am"
#   "You've hit your Opus limit · resets 3:45pm"
# The middot (·) and ascii dashes are both observed; we accept either.
# Preferred path is the structured `rate_limit_event` JSON line; this regex
# is the fallback when the structured event isn't in the log.
CREDIT_LIMIT_RE = re.compile(
    r"You['\u2019]ve hit your(?:\s+(?P<kind>[A-Za-z]+))?\s+limit\s*[\u00B7\-:]?\s*resets\s+(?P<reset>[^\n\r.]+)",
    re.IGNORECASE,
)
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
FALLBACK_WAIT_HOURS = 1

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 3))
DEFAULT_TIMEOUT_MINUTES = int(os.environ.get("DEFAULT_TIMEOUT_MINUTES", 120))
MAX_TIMEOUT_MINUTES = int(os.environ.get("MAX_TIMEOUT_MINUTES", 120))

SYSTEM_CONTEXT_TEMPLATE = """\
## System Context
You are running inside an isolated Docker container on the AI Task Runner platform.
- You are the `taskrunner` user (non-root).
- Your workspace is `/workspace`. All project files are here.
- Current project: `{project_name}` (directory: `/workspace/{project_name}`)
- Your username: `{username}`
- You have no access to host services (Caddy, Docker, DNS, etc).
- You cannot modify infrastructure outside this container.
- Static websites are served automatically: put files in your project directory and they are live at `https://{project_name}.{username}.{domain}`
- Installed skills may provide additional context about what you can do.
- Focus on the task using the tools and files available within your workspace.

## Spawning Sub-Tasks
You can create additional tasks that run independently and in parallel (up to 3 at a time).
Use this when a task involves multiple independent pieces of work — e.g. processing a list of
items where each item can be handled separately.

To enqueue a sub-task (returns the task ID):
```
python /app/enqueue.py --project {project_name} "Your prompt here"
```
Output: `TASK_ID=20260410_031500_ab12`  — capture with:
```
ID=$(python /app/enqueue.py --project {project_name} "prompt" | grep TASK_ID | cut -d= -f2)
```

To wait for sub-tasks to complete:
```
python /app/task_wait.py <task_id1> <task_id2> ...
```
Blocks until all tasks finish. Prints a JSON summary:
`{{"completed": [...], "failed": [...], "timed_out": [...]}}`

Multi-stage orchestration pattern:
1. Do initial work (e.g. search, identify items to process)
2. Spawn a sub-task per item, capturing each task ID
3. Wait for all: `python /app/task_wait.py $ID1 $ID2 $ID3`
4. Check results (read files, query APIs, check databases, etc.)
5. Spawn next wave of sub-tasks for items that succeeded
6. Report summary

When to spawn sub-tasks vs doing the work yourself:
- Spawn sub-tasks when items are independent and could benefit from parallel execution.
- Do the work yourself when steps are sequential or depend on each other's output.
- If asked to "create a task for each" or "run these in parallel", use sub-tasks.
"""


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_projects() -> dict:
    path = CONFIG_DIR / "projects.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("projects", {})


def load_users() -> dict:
    path = CONFIG_DIR / "users.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("users", {})


def load_requirements_manifest(project_dir: str) -> tuple[list, str | None]:
    """Read the project's agent-requirements.yaml.

    Returns (requirements, error). Absent manifest -> ([], None): projects that
    don't declare requirements behave exactly as before. A malformed manifest
    returns ([], "<error>") so the caller can fail the task with a pointer at
    the file instead of crashing the poll loop or silently ignoring it.
    """
    path = Path(project_dir) / MANIFEST_NAME
    if not path.is_file():
        return [], None
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        reqs = data.get("requirements", [])
        if not isinstance(reqs, list):
            return [], f"'requirements' must be a list, got {type(reqs).__name__}"
        return [r for r in reqs if isinstance(r, dict)], None
    except yaml.YAMLError as e:
        return [], f"YAML parse error: {e}"


def _requirement_failure(req: dict, problem: str) -> str:
    """Format one unmet requirement as a self-explanatory failure block."""
    lines = [f"- {req.get('id', 'unnamed requirement')}: {problem}"]
    why = str(req.get("why", "")).strip()
    how = str(req.get("how_to_get", "")).strip()
    if why:
        lines.append(f"  Why it's needed: {why}")
    if how:
        lines.append(f"  How to get it: {how}")
    return "\n".join(lines)


def check_requirements(project_name: str, project_dir: str, env: dict) -> list[str]:
    """Verify the project's declared requirements before spending Claude time.

    Returns one human-readable failure string per unmet requirement (empty
    list = all satisfied, or no manifest).
    """
    reqs, error = load_requirements_manifest(project_dir)
    if error:
        return [f"- {MANIFEST_NAME} in the project repo is invalid ({error}). "
                "Fix the manifest and push/sync the project."]

    mcp_servers = None  # lazy-loaded from the materialized MCP config

    failures = []
    for req in reqs:
        kind = req.get("kind")
        if kind == "mcp_server":
            if mcp_servers is None:
                try:
                    with open(MCP_CONFIG_DIR / f"{project_name}.mcp.json") as f:
                        mcp_servers = json_mod.load(f).get("mcpServers") or {}
                except (OSError, json_mod.JSONDecodeError):
                    mcp_servers = {}
            server_name = req.get("server_name", "")
            if server_name not in mcp_servers:
                if req.get("provider") == "task-runner-self":
                    failures.append(_requirement_failure(
                        req, "the runner's own MCP server should be configured "
                             "automatically — restart or recreate your container, "
                             "or contact support if that doesn't fix it"))
                else:
                    failures.append(_requirement_failure(
                        req, f"MCP server '{server_name}' is not configured"))
        elif kind == "env":
            for var in req.get("vars", []):
                name = var.get("name") if isinstance(var, dict) else str(var)
                if name and name not in env:
                    failures.append(_requirement_failure(
                        req, f"environment variable {name} is not set"))
        elif kind == "file":
            rel = str(req.get("path", ""))
            if not rel or rel.startswith(("/", "~")) or ".." in rel.split("/"):
                failures.append(_requirement_failure(
                    req, f"invalid file path in manifest: {rel!r}"))
            elif not (Path(project_dir) / rel).is_file():
                failures.append(_requirement_failure(
                    req, f"required file {rel} is missing from the project"))
        # Unknown kinds are ignored (forward compatibility with newer manifests).
    return failures


def count_processing() -> int:
    """Count tasks currently being processed."""
    if not PROCESSING_DIR.exists():
        return 0
    return len(list(PROCESSING_DIR.glob("*.md")))


def get_queued_tasks() -> list[Path]:
    """Get queued task files sorted by filename (oldest first)."""
    if not QUEUED_DIR.exists():
        return []
    return sorted(QUEUED_DIR.glob("*.md"), key=lambda p: p.name)


def claim_task(task_path: Path) -> Path | None:
    """Atomically claim a task by renaming it to processing/."""
    dest = PROCESSING_DIR / task_path.name
    try:
        os.rename(task_path, dest)
        return dest
    except FileNotFoundError:
        return None  # Another runner claimed it
    except OSError as e:
        log(f"Error claiming {task_path.name}: {e}")
        return None


def update_frontmatter(path: Path, **updates) -> frontmatter.Post:
    """Update frontmatter fields and write back. Returns the updated post."""
    post = frontmatter.load(str(path))
    for key, value in updates.items():
        post.metadata[key] = value
    with open(path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    return post


def extract_result_summary(log_path: Path) -> str:
    """Extract the result text from a Claude log.

    Logs are NDJSON (one event per line) when produced with
    ``--output-format stream-json``. Walk from the end to find the last
    ``{"type":"result",...}`` event and return its ``result`` field.

    Falls back to parsing the whole file as a single JSON object, to stay
    compatible with logs produced under the previous ``--output-format json``
    setting (useful mid-migration and for any manual tooling).
    """
    try:
        content = log_path.read_text()
    except Exception:
        return ""

    def _truncate(s: str) -> str:
        return s[:500] + "..." if len(s) > 500 else s

    # NDJSON path: last result event wins.
    for line in reversed(content.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json_mod.loads(line)
        except Exception:
            continue
        if obj.get("type") == "result":
            return _truncate(obj.get("result", "") or "")

    # Legacy single-JSON fallback.
    try:
        data = json_mod.loads(content)
        return _truncate(data.get("result", "") or "")
    except Exception:
        return ""


def detect_credit_limit(log_path: Path):
    """Scan a task's log for Claude's rate-limit signals.

    Returns (hit, kind, reset_raw, reset_dt). When `hit` is True, exactly one
    of `reset_raw` (human string for parse_reset_time) or `reset_dt` (precise
    UTC datetime from a structured rate_limit_event) will be set.

    Preference order:
      1. Structured stream-json `rate_limit_event` line (precise epoch).
      2. Legacy human-readable "You've hit your … limit · resets …" string.
    """
    try:
        text = log_path.read_text(errors="replace")
    except Exception:
        return False, None, None, None

    # 1. Structured event — emitted by claude --output-format=stream-json.
    for line in text.splitlines():
        if '"type":"rate_limit_event"' not in line and \
                '"type": "rate_limit_event"' not in line:
            continue
        try:
            obj = json_mod.loads(line)
        except Exception:
            continue
        info = obj.get("rate_limit_info") or {}
        if info.get("status") != "rejected":
            continue
        epoch = info.get("resetsAt")
        kind = info.get("rateLimitType") or "session"
        if epoch:
            try:
                reset_dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
            except Exception:
                continue
            return True, str(kind).lower(), None, reset_dt

    # 2. Fall back to the human-readable error string.
    m = CREDIT_LIMIT_RE.search(text)
    if not m:
        return False, None, None, None
    raw_kind = m.group("kind")
    kind = raw_kind.lower() if raw_kind else "session"
    return True, kind, m.group("reset").strip(), None


def parse_reset_time(reset_raw: str, now_utc: datetime) -> datetime | None:
    """Parse Claude's human-readable reset time string into a UTC datetime.

    Examples: "3:45pm", "12am", "Mon 12:00am", "Tue 3pm". The time is assumed
    to be in UTC (container has no TZ configured). Result is the next future
    occurrence; for weekday-prefixed strings, the next matching weekday.
    """
    s = reset_raw.strip()
    # Drop trailing parenthesised qualifiers like "(UTC)".
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    if not s:
        return None

    parts = s.split(None, 1)
    target_dow = None
    if len(parts) == 2 and parts[0][:3].lower() in WEEKDAYS:
        target_dow = WEEKDAYS[parts[0][:3].lower()]
        time_str = parts[1]
    else:
        time_str = s

    tm = re.match(r"(?i)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", time_str)
    if not tm:
        return None
    hour = int(tm.group(1))
    minute = int(tm.group(2) or "0")
    meridiem = (tm.group(3) or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None

    target = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_dow is not None:
        days_ahead = (target_dow - target.weekday()) % 7
        if days_ahead == 0 and target <= now_utc:
            days_ahead = 7
        target += timedelta(days=days_ahead)
    else:
        if target <= now_utc:
            target += timedelta(days=1)
    return target


def finalize_awaiting(task_path: Path, kind: str, reset_at: datetime,
                       reset_raw: str, exit_code: int | None):
    """Move a task to the appropriate awaiting_* dir with credit_reset_at set."""
    now = datetime.now(timezone.utc).isoformat()
    if kind == "weekly":
        dest_dir = AWAITING_WEEKLY_RESET_DIR
        status = "awaiting_weekly_reset"
    else:
        dest_dir = AWAITING_CREDITS_DIR
        status = "awaiting_credits"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / task_path.name

    updates = {
        "status": status,
        "credit_reset_at": reset_at.isoformat(),
        "credit_reset_message": reset_raw,
        "credit_limit_kind": kind,
        "awaiting_since": now,
    }
    if exit_code is not None:
        updates["exit_code"] = exit_code
    update_frontmatter(task_path, **updates)
    os.rename(task_path, dest)
    log(f"Task {task_path.stem} hit {kind} limit → {status}, retry at {reset_at.isoformat()}")


def _requeue_awaiting_file(task_file: Path, reason: str, fresh_session: bool = False):
    """Move one parked awaiting_* task back to queued.

    Normally the task resumes its existing Claude session (`--resume`). Pass
    fresh_session=True when the account/token changed: that path recreates the
    container and wipes the old session, so a new session id is minted and the
    task re-runs from scratch on the new account (a `--resume` would just fail)."""
    now = datetime.now(timezone.utc)
    QUEUED_DIR.mkdir(parents=True, exist_ok=True)
    post = frontmatter.load(str(task_file))
    updates = {
        "status": "queued",
        "requeued_at": now.isoformat(),
        "requeue_reason": reason,
        # Clear the execution clock. `reap_stale_tasks` measures elapsed as
        # `now - started_at`, so leaving the old value here would charge the
        # time spent parked in awaiting_* against max_timeout_minutes — a task
        # held across a 5-hour credit reset came back already "over limit" and
        # was reaped on release, before running a single token. `started_at` is
        # re-stamped when the task next enters processing; the reaper skips a
        # falsy value, so a queued task is simply not a reap candidate.
        "started_at": None,
        # Keep the original start for observability (first run only).
        "first_started_at": post.metadata.get("first_started_at")
                            or post.metadata.get("started_at"),
    }
    if fresh_session:
        updates["claude_session_id"] = str(uuid.uuid4())
        updates["session_started"] = False
    else:
        updates["session_started"] = True
    update_frontmatter(task_file, **updates)
    os.rename(task_file, QUEUED_DIR / task_file.name)


def requeue_ready_awaiting_tasks():
    """Move tasks from awaiting_* back to queued once their credit_reset_at has passed."""
    now = datetime.now(timezone.utc)
    for awaiting_dir in (AWAITING_CREDITS_DIR, AWAITING_WEEKLY_RESET_DIR):
        if not awaiting_dir.exists():
            continue
        for task_file in awaiting_dir.glob("*.md"):
            try:
                post = frontmatter.load(str(task_file))
                reset_raw = post.metadata.get("credit_reset_at")
                if not reset_raw:
                    continue
                reset_dt = datetime.fromisoformat(reset_raw)
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                if reset_dt > now:
                    continue
                _requeue_awaiting_file(task_file, "credit reset reached")
                log(f"Task {task_file.stem} credit reset reached — re-queued for resume")
            except Exception as e:
                log(f"Error re-queueing {task_file.name}: {e}")


def _current_auth_fingerprint() -> str:
    """Short, non-secret fingerprint of the connected Claude credentials.

    Derived from the OAuth token (or API key) the runner will hand to `claude`.
    Changes iff the account/token changes; empty string if none is configured.
    Never stores or logs the token itself — only a truncated SHA-256 hash."""
    material = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not material:
        return ""
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def clear_credit_hold_on_token_change():
    """Drop a stale credit hold when the connected Claude account changes.

    A parked task's `credit_reset_at` is the reset time of *the account that hit
    the limit*. If the user swaps in a different token (e.g. an account that
    still has credits), that reset time is meaningless — the new account has its
    own credit pool — yet `earliest_pending_credit_hold()` would keep the runner
    paused until the old account's reset. So whenever the token fingerprint
    changes, re-queue every awaiting_* task immediately to retry on the new
    account. Runs each tick (every ~5s), so a token swap un-sticks the runner
    almost at once."""
    fp = _current_auth_fingerprint()
    if not fp:
        return  # No token visible — nothing reliable to compare against.
    try:
        prev = AUTH_FP_FILE.read_text().strip() if AUTH_FP_FILE.exists() else ""
    except Exception:
        prev = ""
    if prev == fp:
        return
    # Only force a re-queue when a *different* account was seen before; on the
    # first-ever observation there is nothing parked under an old account.
    if prev:
        count = 0
        for awaiting_dir in (AWAITING_CREDITS_DIR, AWAITING_WEEKLY_RESET_DIR):
            if not awaiting_dir.exists():
                continue
            for task_file in awaiting_dir.glob("*.md"):
                try:
                    _requeue_awaiting_file(task_file, "claude token changed",
                                           fresh_session=True)
                    count += 1
                except Exception as e:
                    log(f"Error re-queueing {task_file.name} after token change: {e}")
        if count:
            log(f"Claude token changed — cleared credit hold, re-queued {count} "
                f"task(s) to retry on the new account.")
    try:
        AUTH_FP_FILE.write_text(fp)
    except Exception as e:
        log(f"Could not persist Claude auth fingerprint: {e}")


MODEL_DIRECTIVE_RE = re.compile(r"^\s*/model\s+\S+", re.MULTILINE)


def has_model_override(task_prompt: str) -> bool:
    """True if the task prompt's first ~10 lines contain a /model slash command.
    Matches the line-anchored pattern Claude CLI uses for slash commands, so
    detection == what Claude actually treats as a directive."""
    head = "\n".join(task_prompt.splitlines()[:10])
    return MODEL_DIRECTIVE_RE.search(head) is not None


def read_default_model() -> str | None:
    """Read the user's saved default model from config/.default_model, or None."""
    path = CONFIG_DIR / ".default_model"
    if not path.exists():
        return None
    try:
        return path.read_text().strip() or None
    except Exception:
        return None


def earliest_pending_credit_hold() -> datetime | None:
    """Return the earliest future credit_reset_at across all parked tasks,
    or None if no task is currently in a credit hold. Call after
    requeue_ready_awaiting_tasks() — anything still parked has a future reset."""
    now = datetime.now(timezone.utc)
    earliest: datetime | None = None
    for awaiting_dir in (AWAITING_CREDITS_DIR, AWAITING_WEEKLY_RESET_DIR):
        if not awaiting_dir.exists():
            continue
        for task_file in awaiting_dir.glob("*.md"):
            try:
                post = frontmatter.load(str(task_file))
                reset_raw = post.metadata.get("credit_reset_at")
                if not reset_raw:
                    continue
                reset_dt = datetime.fromisoformat(reset_raw)
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                if reset_dt <= now:
                    continue
                if earliest is None or reset_dt < earliest:
                    earliest = reset_dt
            except Exception:
                continue
    return earliest


def looks_like_resume_failure(log_path: Path) -> bool:
    """Heuristic: did a --resume invocation fail because the session was missing?"""
    try:
        text = log_path.read_text(errors="replace").lower()
    except Exception:
        return False
    markers = ("session not found", "no such session", "could not find session",
                "unknown session", "invalid session")
    return any(marker in text for marker in markers)


def finalize_task(task_path: Path, status: str, exit_code: int | None = None,
                  result_summary: str = "", failure_reason: str = ""):
    """Move a task to its final directory and update metadata."""
    now = datetime.now(timezone.utc).isoformat()

    dest_dir = DONE_DIR if status == "done" else FAILED_DIR
    dest = dest_dir / task_path.name

    updates = {"status": status, "completed_at": now}
    if exit_code is not None:
        updates["exit_code"] = exit_code
    if result_summary:
        updates["result_summary"] = result_summary
    if failure_reason:
        updates["failure_reason"] = failure_reason
    update_frontmatter(task_path, **updates)

    os.rename(task_path, dest)
    log(f"Task {task_path.stem} → {status} (exit_code={exit_code})")


ATTACH_MAX_BYTES = 500 * 1024 * 1024  # 500MB per file (zips carry whole image sets)
ATTACH_TIMEOUT_S = 30  # socket-inactivity timeout, not total — big files stream fine


def _attach_dir(project_dir: str, task_id: str) -> Path:
    return Path(project_dir) / ".ai-attachments" / task_id


def _attach_safe_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._") or "file"
    return safe[:120]


def download_attachments(meta: dict, project_dir: str, task_id: str) -> str:
    """Download any task attachments into a per-task dir and return a prompt
    block listing their local paths (+ source URLs). Bytes come only from the
    URLs the caller supplied (public Firebase Storage download URLs — no creds
    needed). Best-effort: a file that fails to download is still listed with its
    URL so the session can fetch it itself."""
    import urllib.request

    attachments = meta.get("attachments") or []
    if not isinstance(attachments, list) or not attachments:
        return ""

    dest = _attach_dir(project_dir, task_id)
    # Fresh dir each run (re-queued tasks re-download).
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest, 0o775)
    except OSError:
        pass

    lines = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        url = att.get("url")
        if not isinstance(url, str) or not url:
            continue
        name = str(att.get("name") or "file")
        ctype = str(att.get("contentType") or "application/octet-stream")
        local_path = dest / _attach_safe_name(name)
        ok = False
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "moment-ai-runner"})
            with urllib.request.urlopen(req, timeout=ATTACH_TIMEOUT_S) as resp:
                total = 0
                with open(local_path, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > ATTACH_MAX_BYTES:
                            raise ValueError("attachment exceeds size limit")
                        f.write(chunk)
            try:
                os.chmod(local_path, 0o664)
            except OSError:
                pass
            ok = True
        except Exception as e:  # noqa: BLE001 — best-effort, keep the URL in the prompt
            log(f"Task {task_id}: could not download attachment {name}: {e}")
            if local_path.exists():
                local_path.unlink(missing_ok=True)

        if ok:
            lines.append(f"- {name} ({ctype}) — local: {local_path} — url: {url}")
        else:
            lines.append(f"- {name} ({ctype}) — url: {url} (download failed; fetch it yourself if needed)")

    if not lines:
        return ""
    return (
        "\n## Attachments\n"
        "The owner attached these files (already saved locally). Read them to do "
        "what they asked. For an image the owner wants shown in the app, use its "
        "`url` (a Firebase Storage URL) directly as the image field value — it "
        "already loads in the installed app.\n" + "\n".join(lines) + "\n"
    )


def cleanup_attachments(project_dir: str, task_id: str) -> None:
    """Remove a task's downloaded attachments (best-effort)."""
    try:
        shutil.rmtree(_attach_dir(project_dir, task_id), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def load_skills(project_dir: str) -> str:
    """Load all installed SKILL.md files and return as prompt context."""
    skills_dir = Path(project_dir).parent / ".skills"
    if not skills_dir.is_dir():
        return ""
    parts = []
    for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
        content = skill_file.read_text().strip()
        if content:
            parts.append(content)
    if not parts:
        return ""
    return "\n## Installed Skills\n\n" + "\n\n---\n\n".join(parts) + "\n"


def run_task(task_path: Path, projects: dict, users: dict):
    """Execute a task via headless Claude Code CLI."""
    post = frontmatter.load(str(task_path))
    meta = post.metadata
    task_id = meta.get("id", task_path.stem)
    project_name = meta.get("project", "")
    username = meta.get("user", "")
    task_prompt = post.content.strip()
    timeout_minutes = meta.get("max_timeout_minutes", DEFAULT_TIMEOUT_MINUTES)

    # Session id: assigned at enqueue time, but lazy-assign as a safety net
    # for tasks created by external producers that didn't set one.
    claude_session_id = meta.get("claude_session_id")
    session_started = bool(meta.get("session_started"))
    if not claude_session_id:
        claude_session_id = str(uuid.uuid4())
        update_frontmatter(task_path,
                            claude_session_id=claude_session_id,
                            session_started=False)
        session_started = False

    log(f"Processing task {task_id}: project={project_name}, user={username}, "
        f"timeout={timeout_minutes}m, session={claude_session_id[:8]}, resume={session_started}")

    # Validate project
    project = projects.get(project_name)
    if not project:
        log(f"Task {task_id}: unknown project '{project_name}'")
        finalize_task(task_path, "failed", exit_code=1)
        return

    # Validate user access
    allowed = project.get("allowed_users", [])
    if username not in allowed:
        log(f"Task {task_id}: user '{username}' not allowed for project '{project_name}'")
        finalize_task(task_path, "failed", exit_code=1)
        return

    # Validate project directory exists
    project_dir = project.get("directory", "")
    if not project_dir or not Path(project_dir).is_dir():
        log(f"Task {task_id}: project directory '{project_dir}' does not exist")
        finalize_task(task_path, "failed", exit_code=1)
        return

    # Check disk usage against limit
    workspace_dir = Path(project_dir).parent
    try:
        result = subprocess.run(["du", "-sm", str(workspace_dir)],
                                capture_output=True, text=True, timeout=10)
        usage_mb = float(result.stdout.split()[0]) if result.returncode == 0 else 0
    except Exception:
        usage_mb = 0
    limit_mb = int(os.environ.get("DISK_LIMIT_MB", "1024"))
    if usage_mb > limit_mb:
        log(f"Task {task_id}: disk limit exceeded ({usage_mb:.0f} MB / {limit_mb} MB)")
        finalize_task(task_path, "failed", exit_code=1,
                      result_summary=f"Disk limit exceeded ({usage_mb:.0f} MB / {limit_mb} MB). "
                                     f"Purchase additional storage in your dashboard at ai-task-runner.com.")
        return

    # Build full prompt: system context + skills + user task
    server_url = os.environ.get("SERVER_URL", "")
    domain = server_url.replace("https://", "").split("/")[0] if server_url else "ai-task-runner.com"
    # domain is "username.ai-task-runner.com", we want just "ai-task-runner.com"
    domain_parts = domain.split(".", 1)
    base_domain = domain_parts[1] if len(domain_parts) > 1 else domain

    # Preflight: if the project declares requirements (agent-requirements.yaml)
    # that aren't configured, fail fast with the manifest's own why/how_to_get
    # text. Otherwise the model starts, discovers mid-task that a tool or
    # credential is missing, and either improvises or stalls — neither of
    # which tells anyone what to fix.
    effective_env = {**os.environ,
                     **{k: str(v) for k, v in project.get("env", {}).items()}}
    req_failures = check_requirements(project_name, project_dir, effective_env)
    if req_failures:
        log(f"Task {task_id}: {len(req_failures)} unmet requirement(s) — failing fast")
        finalize_task(task_path, "failed", exit_code=1, result_summary=(
            f"Task blocked — this project declares requirements ({MANIFEST_NAME}) "
            "that are not configured:\n\n"
            + "\n\n".join(req_failures)
            + f"\n\nConfigure them at https://{base_domain}/dashboard under "
              f"Projects → {project_name} → Requirements."))
        return

    system_context = SYSTEM_CONTEXT_TEMPLATE.format(
        project_name=project_name,
        username=username,
        domain=base_domain,
    )
    skills_context = load_skills(project_dir)

    # Apply the user's default-model preference to the task prompt unless the
    # task already specifies its own /model directive. Injecting into
    # task_prompt (not the assembled prompt) keeps the convention identical
    # to a user-written /model line at the top of their task.
    default_model = read_default_model()
    if default_model and not has_model_override(task_prompt):
        task_prompt = f"/model {default_model};\n\n" + task_prompt

    # Download any attachments locally and describe them in the prompt (cleaned
    # up in the finally below). No-op when the task has none.
    attach_block = download_attachments(meta, project_dir, task_id)

    prompt = system_context + skills_context + "\n## Task\n" + task_prompt + attach_block

    # Mark as processing
    now = datetime.now(timezone.utc).isoformat()
    update_frontmatter(task_path, status="processing", started_at=now)

    # Build environment — run claude as non-root taskrunner user
    env = os.environ.copy()
    env["HOME"] = "/home/taskrunner"
    # Disable Claude Code background tasks. Each task is one-shot headless
    # (`claude -p`): a backgrounded Bash job is killed ~5s after the turn
    # returns and nothing re-invokes the model to "check back", so background
    # work silently dies while the runner still sees exit 0. Forcing everything
    # synchronous makes tasks either finish their work or fail honestly.
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    # Bridge submitter identity into the subprocess. getpass.getuser() returns
    # "taskrunner" on the VPS; consumers that need the actual submitter (e.g.
    # moment-agents stamping `owner` on prospects) read CLAUDE_USER.
    if username:
        env["CLAUDE_USER"] = username
    for key, value in project.get("env", {}).items():
        env[key] = str(value)

    # Prepare log file
    log_path = LOGS_DIR / f"{task_id}.log"

    # Build claude command. On first run of a task we use --session-id to pin
    # the session UUID; on subsequent runs (after a credit-hold re-queue) we
    # use --resume to pick up where we left off.
    session_flag = "--resume" if session_started else "--session-id"
    claude_cmd = ["claude", session_flag, claude_session_id, "-p", prompt,
                  "--output-format", "stream-json", "--verbose",
                  "--dangerously-skip-permissions"]

    # Per-project MCP servers: the hosting platform materializes a claude-format
    # MCP config at /secrets/mcp/<project>.mcp.json (bind-mounted, so it
    # survives container recreates). --strict-mcp-config makes that file the
    # ONLY MCP source, so stray per-user state inside the container (e.g. a
    # hand-edited ~/.claude.json) can never affect tasks.
    mcp_config_path = MCP_CONFIG_DIR / f"{project_name}.mcp.json"
    if mcp_config_path.is_file():
        claude_cmd += ["--mcp-config", str(mcp_config_path), "--strict-mcp-config"]

    # Launch Claude Code. Log file is line-buffered so each NDJSON event
    # flushes to disk immediately — the platform's web dashboard tails this
    # file to show live WIP output.
    try:
        with open(log_path, "w", buffering=1) as log_file:
            result = subprocess.run(
                claude_cmd,
                text=True,
                timeout=timeout_minutes * 60,
                cwd=project_dir,
                env=env,
                user="taskrunner",
                group="taskrunner",
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        exit_code = result.returncode

        # Before calling the run a success or failure, check for Claude
        # credit-limit error. If hit, park the task in awaiting_*.
        if exit_code != 0:
            hit, kind, reset_raw, reset_dt = detect_credit_limit(log_path)
            if hit:
                now_utc = datetime.now(timezone.utc)
                if reset_dt is None:
                    reset_dt = parse_reset_time(reset_raw or "", now_utc)
                if reset_dt is None:
                    reset_dt = now_utc + timedelta(hours=FALLBACK_WAIT_HOURS)
                finalize_awaiting(task_path, kind, reset_dt,
                                  reset_raw or reset_dt.isoformat(), exit_code)
                return

            # If this was a --resume attempt and it looks like the session is
            # gone, fall back to a fresh run once. Record the failure on the task.
            if session_started and not meta.get("resume_failed_at") \
                    and looks_like_resume_failure(log_path):
                now = datetime.now(timezone.utc).isoformat()
                new_session_id = str(uuid.uuid4())
                log(f"Task {task_id}: --resume failed, falling back to fresh run "
                    f"(new session {new_session_id[:8]})")
                update_frontmatter(task_path,
                                    status="queued",
                                    claude_session_id=new_session_id,
                                    session_started=False,
                                    resume_failed_at=now,
                                    resume_failure_reason="session not found on resume")
                QUEUED_DIR.mkdir(parents=True, exist_ok=True)
                os.rename(task_path, QUEUED_DIR / task_path.name)
                return

        status = "done" if exit_code == 0 else "failed"
        result_summary = extract_result_summary(log_path)
        finalize_task(task_path, status, exit_code=exit_code,
                      result_summary=result_summary)

    except subprocess.TimeoutExpired:
        reason = f"Task killed: exceeded {timeout_minutes}-minute time limit."
        log(f"Task {task_id}: {reason}")
        with open(log_path, "a") as f:
            f.write(f"\n\n--- TIMED OUT ---\n{reason}\n")
        finalize_task(task_path, "timed_out", exit_code=-1,
                      failure_reason=reason)

    except Exception as e:
        log(f"Task {task_id}: unexpected error: {e}")
        with open(log_path, "a") as f:
            f.write(f"\n\n--- ERROR ---\n{e}\n")
        finalize_task(task_path, "failed", exit_code=-2)

    finally:
        # Attachments are re-downloaded on any re-queue, so it's always safe to
        # remove them once this run's claude subprocess has finished.
        cleanup_attachments(project_dir, task_id)


def reap_stale_tasks():
    """Move processing tasks that have exceeded MAX_TIMEOUT_MINUTES to failed."""
    if not PROCESSING_DIR.exists():
        return
    now = datetime.now(timezone.utc)
    for task_file in PROCESSING_DIR.glob("*.md"):
        try:
            post = frontmatter.load(str(task_file))
            started = post.metadata.get("started_at")
            if not started:
                continue
            started_dt = datetime.fromisoformat(started)
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=timezone.utc)
            elapsed_min = (now - started_dt).total_seconds() / 60
            timeout = post.metadata.get("max_timeout_minutes", MAX_TIMEOUT_MINUTES)
            # Use whichever is smaller: the task's own timeout or the system max
            effective_timeout = min(timeout, MAX_TIMEOUT_MINUTES)
            if elapsed_min > effective_timeout:
                task_id = post.metadata.get("id", task_file.stem)
                reason = (f"Task killed: exceeded {effective_timeout}-minute time limit "
                          f"(ran for {elapsed_min:.0f} minutes).")
                log(f"Reaping stale task {task_id}: {reason}")
                log_path = LOGS_DIR / f"{task_id}.log"
                with open(log_path, "a") as f:
                    f.write(f"\n\n--- TIMED OUT ---\n{reason}\n")
                finalize_task(task_file, "timed_out", exit_code=-1,
                              failure_reason=reason)
        except Exception as e:
            log(f"Error reaping {task_file.name}: {e}")


def main():
    # Ensure directories exist
    for d in [QUEUED_DIR, PROCESSING_DIR, DONE_DIR, FAILED_DIR,
             AWAITING_CREDITS_DIR, AWAITING_WEEKLY_RESET_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    # If the connected Claude account changed, drop any stale credit hold so
    # parked tasks retry on the new account instead of waiting out the old
    # (out-of-credit) account's reset time.
    clear_credit_hold_on_token_change()

    # Re-queue any credit-held tasks whose reset time has passed
    requeue_ready_awaiting_tasks()

    # Reap any stale tasks before checking capacity
    reap_stale_tasks()

    # Check concurrency limit
    active = count_processing()
    if active >= MAX_CONCURRENT:
        log(f"At capacity: {active}/{MAX_CONCURRENT} tasks processing. Skipping.")
        return

    # User-controlled pause — set via the dashboard. Housekeeping still
    # runs but no new tasks get pulled from queued.
    if (CONFIG_DIR / ".paused").exists():
        log("Paused by user; skipping new task starts.")
        return

    # Don't start new tasks while any task is still in a credit hold. The
    # credit pool is account-wide, so a new run would just hit the same
    # limit and bounce into awaiting_credits too — burning a bit of credit
    # each time and cascading the whole queue into the parking dirs.
    hold_until = earliest_pending_credit_hold()
    if hold_until is not None:
        log(f"Credit hold active until {hold_until.isoformat()}; skipping new task starts.")
        return

    # Load config
    projects = load_projects()
    users = load_users()

    if not projects:
        log("No projects configured in config/projects.yaml")
        return

    # Find and claim a task
    queued = get_queued_tasks()
    if not queued:
        return  # Nothing to do — silent exit

    for task_path in queued:
        claimed = claim_task(task_path)
        if claimed:
            run_task(claimed, projects, users)
            return  # One task per invocation

    log("All queued tasks were claimed by other runners.")


if __name__ == "__main__":
    main()
