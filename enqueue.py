#!/usr/bin/env python3
"""
Enqueue a sub-task from within a running task.

Usage:
    python /app/enqueue.py --project myproject "Do the thing"
    python /app/enqueue.py --project myproject --timeout 30 "Quick task"
"""

import argparse
import os
import secrets
import string
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

TASKS_DIR = Path(__file__).parent / "tasks"
QUEUED_DIR = TASKS_DIR / "queued"

# Inherit username from the parent task's environment or config
CONFIG_DIR = Path(__file__).parent / "config"


def get_username():
    """Get the username from users.yaml config."""
    import yaml
    users_path = CONFIG_DIR / "users.yaml"
    if users_path.exists():
        with open(users_path) as f:
            data = yaml.safe_load(f) or {}
        users = data.get("users", {})
        if users:
            return next(iter(users))
    return "unknown"


def main():
    parser = argparse.ArgumentParser(description="Enqueue a sub-task")
    parser.add_argument("--project", required=True, help="Project name")
    parser.add_argument("--timeout", type=int, default=120,
                        help="Max timeout in minutes (default: 120)")
    parser.add_argument("prompt", help="The task prompt")
    args = parser.parse_args()

    username = get_username()
    now = datetime.now(timezone.utc)
    rand = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    task_id = now.strftime(f"%Y%m%d_%H%M%S_{rand}")

    post = frontmatter.Post(
        content=args.prompt,
        id=task_id,
        user=username,
        project=args.project,
        status="queued",
        created_at=now.isoformat(),
        started_at=None,
        completed_at=None,
        exit_code=None,
        max_timeout_minutes=args.timeout,
        claude_session_id=str(uuid.uuid4()),
        session_started=False,
    )

    QUEUED_DIR.mkdir(parents=True, exist_ok=True)
    task_path = QUEUED_DIR / f"{task_id}.md"
    with open(task_path, "wb") as f:
        frontmatter.dump(post, f)

    print(f"TASK_ID={task_id}")


if __name__ == "__main__":
    main()
