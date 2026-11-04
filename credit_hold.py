"""Credit-hold bookkeeping shared by runner.py and server.py.

A credit hold is in force while at least one task sits in awaiting_credits /
awaiting_weekly_reset with a credit_reset_at still in the future: the runner
starts nothing new until it passes (the usage pool is account-wide, so a new
task would only bounce into the same hold). Both processes need to see it —
the runner to pause, the server to tell a client why its queued task is not
moving — so the lookup lives here and takes the directories as arguments
(each module keeps its own path constants, and their tests patch those).

The marker file records that the webhooks have been told about the hold now
in force, so MAX_CONCURRENT tasks parking on the same limit within seconds of
each other notify once, and the lift of the hold notifies once. It lives under
tasks/ (read-write) because config/ is mounted read-only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import frontmatter

MARKER_NAME = ".credit_hold_notified"


@dataclass(frozen=True)
class Hold:
    """The hold in force: when it lifts, which limit caused it, how many tasks wait."""
    reset_at: datetime
    kind: str
    parked: int


def parse_iso(raw) -> datetime | None:
    """An ISO timestamp as an aware UTC datetime, or None when absent/unparseable."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def earliest_pending(dirs: Iterable[Path], now: datetime | None = None) -> Hold | None:
    """The earliest future credit_reset_at across every parked task, with the
    kind of limit that task hit and how many tasks are parked for the future.
    None when nothing is parked with a future reset (a task past its reset is
    about to be re-queued and holds nothing)."""
    now = now or datetime.now(timezone.utc)
    earliest: datetime | None = None
    kind = "session"
    parked = 0
    for d in dirs:
        d = Path(d)
        if not d.exists():
            continue
        for task_file in d.glob("*.md"):
            try:
                meta = frontmatter.load(str(task_file)).metadata
            except Exception:
                continue
            reset_dt = parse_iso(meta.get("credit_reset_at"))
            if reset_dt is None or reset_dt <= now:
                continue
            parked += 1
            if earliest is None or reset_dt < earliest:
                earliest = reset_dt
                kind = str(meta.get("credit_limit_kind") or "session")
    if earliest is None:
        return None
    return Hold(reset_at=earliest, kind=kind, parked=parked)


def marker_path(tasks_dir: Path) -> Path:
    return Path(tasks_dir) / MARKER_NAME


def read_marker(tasks_dir: Path) -> dict | None:
    """The notified hold ({reset_at, kind, notified_at}), or None when none is recorded."""
    path = marker_path(tasks_dir)
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}  # present but unreadable: still counts as "already notified"


def claim_marker(tasks_dir: Path, reset_at: datetime, kind: str) -> bool:
    """Create the marker if it does not exist. Exclusive create, so of two
    runner processes parking tasks in the same instant exactly one wins and
    sends the notification. False = another process already claimed it."""
    path = marker_path(tasks_dir)
    payload = json.dumps({"reset_at": reset_at.isoformat(), "kind": kind,
                          "notified_at": datetime.now(timezone.utc).isoformat()})
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    return True


def clear_marker(tasks_dir: Path) -> bool:
    """Remove the marker. False when it was already gone (another process got there first)."""
    try:
        os.remove(marker_path(tasks_dir))
        return True
    except FileNotFoundError:
        return False
