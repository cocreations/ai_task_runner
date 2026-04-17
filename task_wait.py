#!/usr/bin/env python3
"""
Wait for sub-tasks to complete.

Usage:
    python /app/task_wait.py <task_id> [task_id ...] [--timeout 120] [--poll 10]

Polls the done/ and failed/ directories until all specified tasks have
finished (or timeout is reached). Prints a JSON summary on completion.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

TASKS_DIR = Path(__file__).parent / "tasks"
DONE_DIR = TASKS_DIR / "done"
FAILED_DIR = TASKS_DIR / "failed"

MAX_TIMEOUT_MINUTES = int(os.environ.get("MAX_TIMEOUT_MINUTES", 120))


def check_task(task_id: str) -> str | None:
    """Check if a task has completed. Returns status or None if still running."""
    for status_dir, status in [(DONE_DIR, "done"), (FAILED_DIR, "failed")]:
        if (status_dir / f"{task_id}.md").exists():
            return status
    return None


def main():
    parser = argparse.ArgumentParser(description="Wait for sub-tasks to complete")
    parser.add_argument("task_ids", nargs="+", help="Task IDs to wait for")
    parser.add_argument("--timeout", type=int, default=MAX_TIMEOUT_MINUTES,
                        help=f"Max wait time in minutes (default: {MAX_TIMEOUT_MINUTES})")
    parser.add_argument("--poll", type=int, default=10,
                        help="Poll interval in seconds (default: 10)")
    args = parser.parse_args()

    deadline = time.time() + args.timeout * 60
    pending = set(args.task_ids)
    results = {"completed": [], "failed": [], "timed_out": []}

    print(f"Waiting for {len(pending)} task(s)...", flush=True)

    while pending and time.time() < deadline:
        for task_id in list(pending):
            status = check_task(task_id)
            if status == "done":
                results["completed"].append(task_id)
                pending.discard(task_id)
                print(f"  {task_id}: done", flush=True)
            elif status == "failed":
                results["failed"].append(task_id)
                pending.discard(task_id)
                print(f"  {task_id}: failed", flush=True)

        if pending:
            time.sleep(args.poll)

    # Any still pending after timeout
    for task_id in pending:
        results["timed_out"].append(task_id)
        print(f"  {task_id}: timed out", flush=True)

    print(f"\nAll tasks resolved. {len(results['completed'])} completed, "
          f"{len(results['failed'])} failed, {len(results['timed_out'])} timed out.", flush=True)
    print(json.dumps(results), flush=True)

    # Exit with error if any failed or timed out
    if results["failed"] or results["timed_out"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
