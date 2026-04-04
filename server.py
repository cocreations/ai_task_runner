"""
Claude Task Runner — MCP Server

Accepts tasks via MCP tools, queues them as markdown files for execution
by headless Claude Code CLI sessions. Authenticates users via API keys
mapped to identities in config/users.yaml.
"""

import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import frontmatter
import yaml
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP, Context

# ── Configuration ──

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
TASKS_DIR = BASE_DIR / "tasks"
LOGS_DIR = BASE_DIR / "logs"

STATUS_DIRS = {
    "queued": TASKS_DIR / "queued",
    "processing": TASKS_DIR / "processing",
    "done": TASKS_DIR / "done",
    "failed": TASKS_DIR / "failed",
}


def load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_users() -> dict:
    """Load users config. Returns {api_key: {name, display_name, api_key}}."""
    data = load_yaml(CONFIG_DIR / "users.yaml")
    keys = {}
    for name, info in data.get("users", {}).items():
        keys[info["api_key"]] = {"name": name, **info}
    return keys


def load_projects() -> dict:
    """Load projects config. Returns {name: {directory, description, env, allowed_users}}."""
    data = load_yaml(CONFIG_DIR / "projects.yaml")
    return data.get("projects", {})


# ── Authentication ──

_user_keys = load_users()
_projects = load_projects()


class ApiKeyVerifier(TokenVerifier):
    """Verifies pre-shared API keys from config/users.yaml."""

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        user = _user_keys.get(token)
        if user is None:
            return None
        return AccessToken(
            token=token,
            client_id=user["name"],
            scopes=[],
        )


def get_current_user() -> str:
    """Get the authenticated username from the current request context."""
    token = get_access_token()
    if token is None:
        raise ValueError("No authenticated user")
    return token.client_id


def user_can_access_project(username: str, project_name: str) -> bool:
    """Check if user is in the project's allowed_users list."""
    project = _projects.get(project_name)
    if not project:
        return False
    return username in project.get("allowed_users", [])


# ── Task helpers ──


def generate_task_id() -> str:
    """Generate a task ID: timestamp + random suffix."""
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    return f"{ts}_{suffix}"


def find_task_file(task_id: str) -> Optional[Path]:
    """Find a task file by ID across all status directories."""
    filename = f"{task_id}.md"
    for status_dir in STATUS_DIRS.values():
        path = status_dir / filename
        if path.exists():
            return path
    return None


def list_task_files(status: str = "", limit: int = 20) -> list[Path]:
    """List task files, optionally filtered by status, newest first."""
    if status and status in STATUS_DIRS:
        dirs = [STATUS_DIRS[status]]
    else:
        dirs = list(STATUS_DIRS.values())

    files = []
    for d in dirs:
        if d.exists():
            files.extend(d.glob("*.md"))

    # Sort by filename (which starts with timestamp) — newest first
    files.sort(key=lambda p: p.name, reverse=True)
    return files[:limit]


# ── MCP Server ──

_server_url = os.environ.get("SERVER_URL", "https://localhost:8080")

verifier = ApiKeyVerifier()

mcp = FastMCP(
    "claude-task-runner",
    instructions="""You are connected to a Claude Task Runner — a system that queues prompts
for headless Claude Code execution on a remote server.

Use submit_task to queue work. The task will be picked up within 1 minute
and executed by Claude Code with full access to the specified project.

Use task_status or list_tasks to check progress. Use task_output to see
results once complete.""",
    token_verifier=verifier,
    auth=AuthSettings(
        issuer_url=_server_url,
        resource_server_url=_server_url,
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8080)),
    streamable_http_path="/",
)


# ── Tools ──


@mcp.tool()
def submit_task(project: str, prompt: str, max_timeout_minutes: int = 60) -> str:
    """Submit a task for headless Claude Code execution.

    The task will be queued and picked up within 1 minute by the runner.

    Args:
        project: Project name (from projects.yaml) — determines working directory.
        prompt: The prompt/instructions for Claude Code to execute.
        max_timeout_minutes: Maximum execution time (default 60, max 180).
    """
    username = get_current_user()

    # Validate project access
    if project not in _projects:
        available = [p for p in _projects if user_can_access_project(username, p)]
        return f"Unknown project '{project}'. Available: {', '.join(available) or 'none'}"

    if not user_can_access_project(username, project):
        return f"You don't have access to project '{project}'."

    # Validate timeout
    max_timeout_minutes = max(1, min(max_timeout_minutes, 180))

    # Create task file
    task_id = generate_task_id()
    now = datetime.now(timezone.utc).isoformat()

    post = frontmatter.Post(
        content=prompt,
        id=task_id,
        user=username,
        project=project,
        status="queued",
        created_at=now,
        started_at=None,
        completed_at=None,
        exit_code=None,
        max_timeout_minutes=max_timeout_minutes,
    )

    task_path = STATUS_DIRS["queued"] / f"{task_id}.md"
    with open(task_path, "wb") as f:
        frontmatter.dump(post, f)

    return f"Task queued: {task_id}\nProject: {project}\nTimeout: {max_timeout_minutes}m\nIt will be picked up within 1 minute."


@mcp.tool()
def task_status(task_id: str) -> str:
    """Check the status of a task by its ID.

    Returns the task's metadata (status, timestamps, exit code) and the prompt.
    """
    get_current_user()  # ensure authenticated

    path = find_task_file(task_id)
    if not path:
        return f"Task '{task_id}' not found."

    post = frontmatter.load(str(path))
    meta = post.metadata

    lines = [
        f"Task: {meta.get('id', task_id)}",
        f"Status: {meta.get('status', 'unknown')}",
        f"User: {meta.get('user', '?')}",
        f"Project: {meta.get('project', '?')}",
        f"Created: {meta.get('created_at', '?')}",
    ]
    if meta.get("started_at"):
        lines.append(f"Started: {meta['started_at']}")
    if meta.get("completed_at"):
        lines.append(f"Completed: {meta['completed_at']}")
    if meta.get("exit_code") is not None:
        lines.append(f"Exit code: {meta['exit_code']}")
    lines.append(f"Timeout: {meta.get('max_timeout_minutes', 60)}m")
    lines.append(f"\n--- Prompt ---\n{post.content}")

    return "\n".join(lines)


@mcp.tool()
def list_tasks(status: str = "", limit: int = 20) -> str:
    """List recent tasks, optionally filtered by status.

    Args:
        status: Filter by status — 'queued', 'processing', 'done', 'failed'. Empty = all.
        limit: Maximum number of tasks to return (default 20).
    """
    get_current_user()

    if status and status not in STATUS_DIRS:
        return f"Invalid status '{status}'. Use: queued, processing, done, failed"

    files = list_task_files(status=status, limit=limit)
    if not files:
        return "No tasks found" + (f" with status '{status}'" if status else "") + "."

    lines = []
    for path in files:
        post = frontmatter.load(str(path))
        meta = post.metadata
        line = f"[{meta.get('status', '?')}] {meta.get('id', path.stem)} — {meta.get('project', '?')}"
        if meta.get("user"):
            line += f" (by {meta['user']})"
        # Show first 80 chars of prompt
        preview = post.content.strip()[:80].replace("\n", " ")
        if len(post.content.strip()) > 80:
            preview += "..."
        line += f"\n  {preview}"
        lines.append(line)

    return "\n\n".join(lines)


@mcp.tool()
def cancel_task(task_id: str) -> str:
    """Cancel a queued task. Only tasks that haven't started processing can be cancelled.

    Args:
        task_id: The task ID to cancel.
    """
    get_current_user()

    # Only look in queued directory
    queued_path = STATUS_DIRS["queued"] / f"{task_id}.md"
    if not queued_path.exists():
        # Check if it's in another state
        path = find_task_file(task_id)
        if path:
            post = frontmatter.load(str(path))
            return f"Cannot cancel — task is '{post.metadata.get('status', 'unknown')}'. Only queued tasks can be cancelled."
        return f"Task '{task_id}' not found."

    # Move to failed with status cancelled
    post = frontmatter.load(str(queued_path))
    post.metadata["status"] = "cancelled"
    post.metadata["completed_at"] = datetime.now(timezone.utc).isoformat()

    failed_path = STATUS_DIRS["failed"] / f"{task_id}.md"
    with open(failed_path, "wb") as f:
        frontmatter.dump(post, f)
    queued_path.unlink()

    return f"Task {task_id} cancelled."


@mcp.tool()
def task_output(task_id: str) -> str:
    """Get the execution output (log) for a completed or failed task.

    Args:
        task_id: The task ID to get output for.
    """
    get_current_user()

    log_path = LOGS_DIR / f"{task_id}.log"
    if not log_path.exists():
        # Check if task exists at all
        path = find_task_file(task_id)
        if not path:
            return f"Task '{task_id}' not found."
        post = frontmatter.load(str(path))
        status = post.metadata.get("status", "unknown")
        if status in ("queued", "processing"):
            return f"Task is still '{status}' — output not yet available."
        return f"No log file found for task '{task_id}'."

    content = log_path.read_text()
    if len(content) > 50000:
        # Truncate very large logs — show last 50k chars
        content = "... (truncated, showing last 50000 chars) ...\n" + content[-50000:]

    return content


@mcp.tool()
def list_projects() -> str:
    """List available projects that you can submit tasks to.

    Returns project names, descriptions, and your access level.
    """
    username = get_current_user()

    lines = []
    for name, config in _projects.items():
        accessible = user_can_access_project(username, name)
        status = "accessible" if accessible else "no access"
        line = f"[{status}] {name}"
        if config.get("description"):
            line += f" — {config['description']}"
        line += f"\n  Directory: {config.get('directory', '?')}"
        lines.append(line)

    if not lines:
        return "No projects configured."

    return "\n\n".join(lines)


# ── Entry point ──

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    mcp.run(transport=transport)
