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

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import yaml

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
TASKS_DIR = BASE_DIR / "tasks"
LOGS_DIR = BASE_DIR / "logs"

QUEUED_DIR = TASKS_DIR / "queued"
PROCESSING_DIR = TASKS_DIR / "processing"
DONE_DIR = TASKS_DIR / "done"
FAILED_DIR = TASKS_DIR / "failed"

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 2))
DEFAULT_TIMEOUT_MINUTES = int(os.environ.get("DEFAULT_TIMEOUT_MINUTES", 60))


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
    with open(path, "wb") as f:
        frontmatter.dump(post, f)
    return post


def finalize_task(task_path: Path, status: str, exit_code: int | None = None):
    """Move a task to its final directory and update metadata."""
    now = datetime.now(timezone.utc).isoformat()

    dest_dir = DONE_DIR if status == "done" else FAILED_DIR
    dest = dest_dir / task_path.name

    updates = {"status": status, "completed_at": now}
    if exit_code is not None:
        updates["exit_code"] = exit_code
    update_frontmatter(task_path, **updates)

    os.rename(task_path, dest)
    log(f"Task {task_path.stem} → {status} (exit_code={exit_code})")


def run_task(task_path: Path, projects: dict, users: dict):
    """Execute a task via headless Claude Code CLI."""
    post = frontmatter.load(str(task_path))
    meta = post.metadata
    task_id = meta.get("id", task_path.stem)
    project_name = meta.get("project", "")
    username = meta.get("user", "")
    prompt = post.content.strip()
    timeout_minutes = meta.get("max_timeout_minutes", DEFAULT_TIMEOUT_MINUTES)

    log(f"Processing task {task_id}: project={project_name}, user={username}, timeout={timeout_minutes}m")

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

    # Mark as processing
    now = datetime.now(timezone.utc).isoformat()
    update_frontmatter(task_path, status="processing", started_at=now)

    # Build environment
    env = os.environ.copy()
    for key, value in project.get("env", {}).items():
        env[key] = str(value)

    # Prepare log file
    log_path = LOGS_DIR / f"{task_id}.log"

    # Launch Claude Code
    # Pass prompt as positional arg to `claude -p`. For very long prompts
    # (>128KB), pipe via stdin instead: ["claude", "-p"], input=prompt
    try:
        with open(log_path, "w") as log_file:
            result = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json"],
                text=True,
                timeout=timeout_minutes * 60,
                cwd=project_dir,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        exit_code = result.returncode
        status = "done" if exit_code == 0 else "failed"
        finalize_task(task_path, status, exit_code=exit_code)

    except subprocess.TimeoutExpired:
        log(f"Task {task_id}: timed out after {timeout_minutes}m")
        # Write timeout notice to log
        with open(log_path, "a") as f:
            f.write(f"\n\n--- TIMEOUT ---\nTask killed after {timeout_minutes} minutes.\n")
        finalize_task(task_path, "timeout", exit_code=-1)

    except Exception as e:
        log(f"Task {task_id}: unexpected error: {e}")
        with open(log_path, "a") as f:
            f.write(f"\n\n--- ERROR ---\n{e}\n")
        finalize_task(task_path, "failed", exit_code=-2)


def main():
    # Ensure directories exist
    for d in [QUEUED_DIR, PROCESSING_DIR, DONE_DIR, FAILED_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    # Check concurrency limit
    active = count_processing()
    if active >= MAX_CONCURRENT:
        log(f"At capacity: {active}/{MAX_CONCURRENT} tasks processing. Skipping.")
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
