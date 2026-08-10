#!/usr/bin/env python3
"""
Claude Task Runner — Housekeeping

Called from runner.py on every poll tick, self-throttled via a stamp file so
the real work only runs every HOUSEKEEPING_INTERVAL_MINUTES.

Why this exists: tasks can daemonize processes (nohup/setsid a game server,
a watcher, etc.) that outlive the `claude -p` invocation. Those orphans get
reparented to PID 1 and nothing ever stops them — one leaked Godot server
once wrote an 18 GB log into /tmp and filled the host disk. Housekeeping
enforces two rules:

  1. Runaway logs are truncated before they can fill the disk, and old task
     logs are eventually deleted.
  2. When no task is running, no task-spawned process should be running
     either — leftovers are killed and stale /tmp debris is removed.

All thresholds are env-overridable; every action is logged.
"""

import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
TASKS_DIR = BASE_DIR / "tasks"
LOGS_DIR = BASE_DIR / "logs"
PROCESSING_DIR = TASKS_DIR / "processing"
QUEUED_DIR = TASKS_DIR / "queued"

STAMP_FILE = TASKS_DIR / ".housekeeping_stamp"

INTERVAL_MINUTES = int(os.environ.get("HOUSEKEEPING_INTERVAL_MINUTES", 10))

# Any *.log file under these roots bigger than this is truncated in place.
LOG_TRUNCATE_MB = int(os.environ.get("HOUSEKEEPING_LOG_TRUNCATE_MB", 256))
LOG_SCAN_ROOTS = ["/tmp", "/home/taskrunner"]

# The runner's own append-forever log gets the same in-place treatment.
RUNNER_LOG_MAX_MB = int(os.environ.get("HOUSEKEEPING_RUNNER_LOG_MAX_MB", 50))

# Task logs older than this are deleted (task_output for such old tasks
# will just report the log as gone).
LOG_RETENTION_DAYS = int(os.environ.get("HOUSEKEEPING_LOG_RETENTION_DAYS", 30))

# Idle-only: top-level /tmp entries owned by taskrunner and untouched for
# this long are removed.
TMP_MAX_AGE_DAYS = int(os.environ.get("HOUSEKEEPING_TMP_MAX_AGE_DAYS", 7))

TASK_USER = os.environ.get("TASK_USER", "taskrunner")


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [housekeeping] {msg}", flush=True)


def _task_uid() -> int | None:
    try:
        import pwd
        return pwd.getpwnam(TASK_USER).pw_uid
    except Exception:
        return None


def _no_tasks_running() -> bool:
    """True when nothing is processing — i.e. any task-user process is a leak.

    Processing tasks are visible as files in tasks/processing/ from the moment
    they're claimed (before the claude subprocess starts), so an empty dir
    means no task-spawned process can legitimately exist.
    """
    if not PROCESSING_DIR.exists():
        return True
    return not any(PROCESSING_DIR.glob("*.md"))


def _iter_task_user_pids(uid: int):
    """Yield (pid, comm) for every live, non-zombie process owned by uid."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            status = Path(f"/proc/{pid}/status").read_text()
        except OSError:
            continue  # exited between listdir and read
        p_uid = None
        state = ""
        comm = ""
        for line in status.splitlines():
            if line.startswith("Uid:"):
                p_uid = int(line.split()[1])
            elif line.startswith("State:"):
                state = line.split()[1]
            elif line.startswith("Name:"):
                comm = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
        if p_uid == uid and state != "Z":
            yield pid, comm


def reap_orphan_processes():
    """Kill task-user processes when no task is running.

    Tasks run as TASK_USER; the runner and MCP server run as root. So with an
    empty processing dir, every TASK_USER process is an escaped daemon from a
    finished task. TERM first, KILL what survives.
    """
    uid = _task_uid()
    if uid is None:
        return
    victims = list(_iter_task_user_pids(uid))
    if not victims:
        return
    for pid, comm in victims:
        _log(f"Killing orphan process {pid} ({comm}) — no task is running")
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    time.sleep(2)
    for pid, comm in victims:
        try:
            os.kill(pid, 9)
        except OSError:
            pass  # already gone


def truncate_runaway_logs():
    """In-place truncate any oversized *.log under the scan roots.

    os.truncate (not delete-and-recreate) on purpose: a live writer keeps its
    fd, and deleting the file would leave it appending to an unlinked inode —
    still eating disk, now invisibly. Truncation shrinks the file under the
    writer; O_APPEND writers just continue from the new end.
    """
    limit = LOG_TRUNCATE_MB * 1024 * 1024
    for root in LOG_SCAN_ROOTS:
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            for name in filenames:
                if not name.endswith(".log"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                    if size > limit:
                        os.truncate(path, 0)
                        _log(f"Truncated runaway log {path} ({size // (1024*1024)} MB)")
                except OSError:
                    continue


def rotate_runner_log():
    """Cap the runner's own append-only log."""
    path = LOGS_DIR / "runner.log"
    try:
        size = path.stat().st_size
        if size > RUNNER_LOG_MAX_MB * 1024 * 1024:
            os.truncate(path, 0)
            _log(f"Truncated {path} ({size // (1024*1024)} MB)")
    except OSError:
        pass


def purge_old_task_logs():
    """Delete task logs past the retention window."""
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    if not LOGS_DIR.exists():
        return
    removed = 0
    for path in LOGS_DIR.glob("*.log"):
        if path.name == "runner.log":
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        _log(f"Deleted {removed} task log(s) older than {LOG_RETENTION_DAYS} days")


def clean_stale_tmp():
    """Idle-only: remove old taskrunner-owned debris from /tmp."""
    uid = _task_uid()
    if uid is None:
        return
    cutoff = time.time() - TMP_MAX_AGE_DAYS * 86400
    for entry in Path("/tmp").iterdir():
        try:
            st = entry.lstat()
            if st.st_uid != uid or st.st_mtime > cutoff:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            _log(f"Removed stale /tmp entry {entry} "
                 f"(untouched {TMP_MAX_AGE_DAYS}+ days)")
        except OSError:
            continue


def run():
    """Entry point, called every runner tick. Self-throttles via stamp file."""
    try:
        now = time.time()
        try:
            last = STAMP_FILE.stat().st_mtime
        except OSError:
            last = 0
        if now - last < INTERVAL_MINUTES * 60:
            return
        # Stamp first so concurrent runner invocations don't double-run, and a
        # crash below can't put housekeeping into a tight retry loop.
        STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STAMP_FILE.touch()

        # Safe while tasks are running:
        truncate_runaway_logs()
        rotate_runner_log()
        purge_old_task_logs()

        # Only safe when nothing is processing:
        if _no_tasks_running():
            reap_orphan_processes()
            clean_stale_tmp()
    except Exception as e:  # noqa: BLE001 — housekeeping must never break the runner
        _log(f"housekeeping error (continuing): {e}")


if __name__ == "__main__":
    run()
