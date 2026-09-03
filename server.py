"""
Claude Task Runner — MCP Server

Accepts tasks via MCP tools, queues them as markdown files for execution
by headless Claude Code CLI sessions. Authenticates users via API keys
mapped to identities in config/users.yaml, with OAuth 2.0 for claude.ai connectors.
"""

import ipaddress
import os
import secrets
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import frontmatter
import yaml
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP

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


class TaskRunnerOAuthProvider(OAuthAuthorizationServerProvider):
    """
    Minimal OAuth provider for claude.ai MCP connector integration.

    Flow:
    1. Claude.ai registers as a client (dynamic registration)
    2. Claude.ai redirects user to /authorize
    3. Server shows "enter your API key" page
    4. User enters key, server validates, generates auth code
    5. Claude.ai exchanges code for access token at /token
    6. Setup token = the user's API key (permanent, no refresh needed)
    """

    def __init__(self):
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        # Map auth codes to the validated API key
        self._code_to_api_key: dict[str, str] = {}
        # Track issued access tokens
        self._access_tokens: dict[str, AccessToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Return a URL for the API key entry page."""
        # Store the auth params so we can complete the flow later
        # Generate a session token for the auth page
        session = secrets.token_urlsafe(32)

        server_url = os.environ.get("SERVER_URL", "https://localhost:8080")
        # Build URL to our auth page with all needed params
        auth_url = (
            f"{server_url}/auth-page"
            f"?session={session}"
            f"&client_id={client.client_id}"
            f"&redirect_uri={params.redirect_uri}"
            f"&code_challenge={params.code_challenge}"
        )
        if params.state:
            auth_url += f"&state={params.state}"

        return auth_url

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._auth_codes.get(authorization_code)
        if code and code.client_id == client.client_id:
            return code
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        api_key = self._code_to_api_key.pop(authorization_code.code, None)
        if not api_key:
            from mcp.server.auth.provider import TokenError, TokenErrorCode
            raise TokenError(error=TokenErrorCode.INVALID_GRANT, error_description="Invalid auth code")

        # Clean up the auth code
        self._auth_codes.pop(authorization_code.code, None)

        # Store the access token for later verification
        user = _user_keys.get(api_key)
        self._access_tokens[api_key] = AccessToken(
            token=api_key,
            client_id=user["name"] if user else "unknown",
            scopes=[],
        )

        return OAuthToken(
            access_token=api_key,
            token_type="Bearer",
            expires_in=31536000,  # 1 year
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return None  # We don't use refresh tokens

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        from mcp.server.auth.provider import TokenError, TokenErrorCode
        raise TokenError(error=TokenErrorCode.INVALID_GRANT, error_description="Refresh not supported")

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Check if it's a valid API key
        user = _user_keys.get(token)
        if user:
            return AccessToken(
                token=token,
                client_id=user["name"],
                scopes=[],
            )
        return self._access_tokens.get(token)

    async def revoke_token(self, token) -> None:
        if hasattr(token, 'token'):
            self._access_tokens.pop(token.token, None)

    def complete_authorization(
        self, api_key: str, client_id: str, redirect_uri: str,
        code_challenge: str, state: str | None
    ) -> str | None:
        """Validate API key and generate auth code. Returns redirect URL or None."""
        user = _user_keys.get(api_key)
        if not user:
            return None

        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=[],
            expires_at=time.time() + 300,  # 5 min
            client_id=client_id,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=True,
        )
        self._code_to_api_key[code] = api_key

        # Write marker so the platform knows MCP has been connected
        marker = TASKS_DIR / ".mcp_connected"
        marker.write_text(datetime.now(timezone.utc).isoformat())

        return construct_redirect_uri(redirect_uri, code=code, state=state)


oauth_provider = TaskRunnerOAuthProvider()


def get_current_user() -> str:
    """Get the authenticated username from the current request context."""
    from mcp.server.auth.middleware.auth_context import get_access_token
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


def _build_identity() -> dict:
    """Identify this server: URL, hostname, and deployment type.

    Deployment is taken from the DEPLOYMENT env var if set; otherwise
    auto-detected as 'local' for loopback / RFC1918 hosts, else 'self-hosted'.
    The platform wrapper sets DEPLOYMENT=hosted explicitly.
    """
    hostname = urlparse(_server_url).hostname or "unknown"

    deployment = os.environ.get("DEPLOYMENT", "").strip().lower()
    if not deployment:
        deployment = "self-hosted"
        if hostname in ("localhost", "127.0.0.1", "::1"):
            deployment = "local"
        else:
            try:
                if ipaddress.ip_address(hostname).is_private:
                    deployment = "local"
            except ValueError:
                pass

    return {
        "server_url": _server_url,
        "hostname": hostname,
        "deployment": deployment,
    }


_identity = _build_identity()

mcp = FastMCP(
    "claude-task-runner",
    instructions=f"""You are connected to a Claude Task Runner — a system that queues prompts
for headless Claude Code execution on a remote server.

Use submit_task to queue work. The task will be picked up within seconds
and executed by Claude Code with full access to the specified project.

Use task_status or list_tasks to check progress. Use task_output to see
results once complete.

This server identifies as: {_identity['hostname']} ({_identity['deployment']} deployment, URL: {_identity['server_url']}).
Call the server_info tool any time to re-check this identity.""",
    auth_server_provider=oauth_provider,
    auth=AuthSettings(
        issuer_url=_server_url,
        resource_server_url=_server_url,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
        ),
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8080)),
    streamable_http_path="/",
)


# ── Auth page route ──

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse


AUTH_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Connect to AI Task Runner</title>
    <style>
        body {{ font-family: system-ui, sans-serif; background: #0f0d1a; color: #e8e6f0;
               display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
        .card {{ background: #1a1730; border: 1px solid rgba(255,255,255,0.12); border-radius: 16px;
                 padding: 2rem; max-width: 420px; width: 100%; }}
        h2 {{ color: #fff; margin-top: 0; }}
        p {{ color: #a09bb5; font-size: 0.9rem; line-height: 1.5; }}
        input {{ width: 100%; padding: 0.6rem; border-radius: 8px; border: 1px solid rgba(255,255,255,0.2);
                 background: #0f0d1a; color: #e8e6f0; font-size: 1rem; box-sizing: border-box; }}
        button {{ width: 100%; padding: 0.7rem; border-radius: 8px; border: none; background: #818cf8;
                  color: #fff; font-size: 1rem; cursor: pointer; margin-top: 1rem; }}
        button:hover {{ background: #6366f1; }}
        .error {{ color: #fca5a5; font-size: 0.85rem; margin-top: 0.5rem; }}
    </style>
</head>
<body>
    <div class="card">
        <h2>Connect to AI Task Runner</h2>
        <p>Enter your API key to authorize claude.ai to access your task runner.
           You can find this on your <strong>Setup</strong> page at ai-task-runner.com.</p>
        {error}
        <form method="post">
            <input type="hidden" name="client_id" value="{client_id}">
            <input type="hidden" name="redirect_uri" value="{redirect_uri}">
            <input type="hidden" name="code_challenge" value="{code_challenge}">
            <input type="hidden" name="state" value="{state}">
            <label style="display:block; margin-bottom: 0.5rem; color: #a09bb5; font-size: 0.85rem;">API Key</label>
            <input type="password" name="api_key" placeholder="Your API key from the Setup page" required autofocus>
            <button type="submit">Authorize</button>
        </form>
    </div>
</body>
</html>"""


async def auth_page_get(request: Request) -> HTMLResponse:
    """Show the API key entry form."""
    params = request.query_params
    return HTMLResponse(AUTH_PAGE_HTML.format(
        client_id=params.get("client_id", ""),
        redirect_uri=params.get("redirect_uri", ""),
        code_challenge=params.get("code_challenge", ""),
        state=params.get("state", ""),
        error="",
    ))


async def auth_page_post(request: Request) -> HTMLResponse | RedirectResponse:
    """Handle API key submission."""
    form = await request.form()
    api_key = form.get("api_key", "").strip()
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    code_challenge = form.get("code_challenge", "")
    state = form.get("state", "") or None

    redirect_url = oauth_provider.complete_authorization(
        api_key=api_key,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
    )

    if not redirect_url:
        return HTMLResponse(AUTH_PAGE_HTML.format(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state or "",
            error='<p class="error">Invalid API key. Check your Setup page at ai-task-runner.com.</p>',
        ))

    return RedirectResponse(url=redirect_url, status_code=302)


@mcp.custom_route("/auth-page", methods=["GET"])
async def auth_page_get_route(request: Request) -> HTMLResponse:
    return await auth_page_get(request)


@mcp.custom_route("/auth-page", methods=["POST"])
async def auth_page_post_route(request: Request):
    return await auth_page_post(request)


# ── Tools ──


@mcp.tool()
def submit_task(
    project: str,
    prompt: str,
    max_timeout_minutes: int = 120,
    claude_session_id: str = "",
    session_started: bool = False,
    attachments: list | None = None,
) -> str:
    """Submit a task for headless Claude Code execution.

    The task will be queued and picked up within seconds by the runner.

    Args:
        project: Project name (from projects.yaml) — determines working directory.
        prompt: The prompt/instructions for Claude Code to execute.
        max_timeout_minutes: Maximum execution time (default 120, max 180).
        claude_session_id: Optional. Pin the Claude Code session to this id so a
            series of tasks form ONE continuous conversation. Omit for a fresh
            one-off session (the default).
        session_started: Optional. When claude_session_id is set, pass False on
            the first task (the runner uses --session-id to create it) and True
            on subsequent tasks (the runner uses --resume to continue it).
        attachments: Optional. A list of {name, contentType, url} objects. The
            runner downloads each url into a per-task directory and lists the
            local paths in the prompt so the session can read/view them. Only
            metadata + URLs travel here — never file bytes.
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

    fields = dict(
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
    # Session continuity (optional): the runner already honors these frontmatter
    # keys — claude_session_id + session_started drive --session-id vs --resume.
    if claude_session_id:
        fields["claude_session_id"] = claude_session_id
        fields["session_started"] = bool(session_started)

    # Attachments (optional): keep only the fields the runner needs, and only
    # well-formed entries. Bytes never travel here — just metadata + a URL the
    # runner fetches.
    if attachments:
        clean = []
        for a in attachments:
            if isinstance(a, dict) and isinstance(a.get("url"), str) and a["url"]:
                clean.append({
                    "name": str(a.get("name") or "file")[:200],
                    "contentType": str(a.get("contentType") or "application/octet-stream"),
                    "url": a["url"],
                })
        if clean:
            fields["attachments"] = clean

    post = frontmatter.Post(content=prompt, **fields)

    task_path = STATUS_DIRS["queued"] / f"{task_id}.md"
    with open(task_path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))

    return f"Task queued: {task_id}\nProject: {project}\nTimeout: {max_timeout_minutes}m\nIt will be picked up within seconds."


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
    with open(failed_path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
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
        # No log file means the task failed BEFORE the worker started — a
        # pre-flight rejection (disk limit, unmet requirements, bad project/user
        # …). The runner records why in result_summary; surface that instead of a
        # bare "no log", which reads as a crash and hides an actionable message.
        summary = post.metadata.get("result_summary")
        exit_code = post.metadata.get("exit_code")
        header = f"Task '{task_id}' {status}"
        if exit_code is not None:
            header += f" (exit {exit_code})"
        header += " — it failed before execution started, so there is no run log."
        if summary:
            return f"{header}\n\n{summary}"
        return header + " No further detail was recorded."

    content = log_path.read_text()
    if len(content) > 50000:
        # Truncate very large logs — show last 50k chars
        content = "... (truncated, showing last 50000 chars) ...\n" + content[-50000:]

    return content


@mcp.tool()
def server_info() -> str:
    """Identify which AI Task Runner MCP server you are connected to.

    Returns the server's hostname, public URL, and deployment type
    (hosted = ai-task-runner.com platform / self-hosted = user's own VPS / local = dev).
    Useful when the user asks "which server am I on" or before a task that
    depends on knowing the environment.
    """
    info = _build_identity()
    return (
        f"Server hostname: {info['hostname']}\n"
        f"Server URL: {info['server_url']}\n"
        f"Deployment: {info['deployment']}"
    )


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


# ── CORS (allow browser dashboards to call MCP tools) ──

from starlette.middleware.cors import CORSMiddleware

CORS_ORIGINS = [
    "https://pipeline.momentevents.co",
    "https://moment-pipeline-dashboard.web.app",
    "https://spellgrove.com",  # the Lor site's "Ask an agent" panel (2026-09-03)
    "http://localhost:5173",
    "http://localhost:5174",
]
# Extra browser origins without a rebuild: CORS_EXTRA_ORIGINS="https://a.example,https://b.example"
CORS_ORIGINS += [o.strip() for o in os.environ.get("CORS_EXTRA_ORIGINS", "").split(",") if o.strip()]

# ── Entry point ──

if __name__ == "__main__":
    app = mcp.streamable_http_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "Mcp-Session-Id"],
        expose_headers=["Mcp-Session-Id"],
    )
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
