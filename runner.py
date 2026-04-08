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

import json as json_mod
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


def extract_result_summary(log_path: Path) -> str:
    """Extract the result text from a Claude JSON log."""
    try:
        content = log_path.read_text()
        data = json_mod.loads(content)
        result = data.get("result", "")
        if len(result) > 500:
            result = result[:500] + "..."
        return result
    except Exception:
        return ""


def finalize_task(task_path: Path, status: str, exit_code: int | None = None,
                  result_summary: str = ""):
    """Move a task to its final directory and update metadata."""
    now = datetime.now(timezone.utc).isoformat()

    dest_dir = DONE_DIR if status == "done" else FAILED_DIR
    dest = dest_dir / task_path.name

    updates = {"status": status, "completed_at": now}
    if exit_code is not None:
        updates["exit_code"] = exit_code
    if result_summary:
        updates["result_summary"] = result_summary
    update_frontmatter(task_path, **updates)

    os.rename(task_path, dest)
    log(f"Task {task_path.stem} → {status} (exit_code={exit_code})")


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

    system_context = SYSTEM_CONTEXT_TEMPLATE.format(
        project_name=project_name,
        username=username,
        domain=base_domain,
    )
    skills_context = load_skills(project_dir)
    prompt = system_context + skills_context + "\n## Task\n" + task_prompt

    # Mark as processing
    now = datetime.now(timezone.utc).isoformat()
    update_frontmatter(task_path, status="processing", started_at=now)

    # Build environment — run claude as non-root taskrunner user
    env = os.environ.copy()
    env["HOME"] = "/home/taskrunner"
    for key, value in project.get("env", {}).items():
        env[key] = str(value)

    # Prepare log file
    log_path = LOGS_DIR / f"{task_id}.log"

    # Launch Claude Code
    try:
        with open(log_path, "w") as log_file:
            result = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"],
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
        status = "done" if exit_code == 0 else "failed"
        result_summary = extract_result_summary(log_path)
        finalize_task(task_path, status, exit_code=exit_code,
                      result_summary=result_summary)

    except subprocess.TimeoutExpired:
        log(f"Task {task_id}: timed out after {timeout_minutes}m")
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
