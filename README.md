# Claude Task Runner

A generic, open-source, self-hosted system that accepts tasks via MCP, queues them as markdown files, and executes them via headless Claude Code CLI sessions.

```
Your laptop                         Your server ($4.50/mo VPS)
┌──────────┐     SSH + SCP          ┌─────────────────────────┐
│ setup.sh │ ──────────────────────→│ docker compose up -d    │
│ (wizard) │  pushes config +       │  ├─ caddy (HTTPS)       │
│          │  starts container      │  └─ task-runner          │
└──────────┘                        │     ├─ server.py (MCP)  │
                                    │     ├─ cron → runner.py │
                                    │     └─ claude CLI       │
                                    └─────────────────────────┘
```

> **Security Warning:** This system executes AI-generated code on your server. Run on a **dedicated machine**. Your Claude subscription or API key has billing/usage implications — monitor it. MCP API keys grant the ability to run arbitrary prompts — treat them like passwords. Your local machine is the admin console — secure it accordingly.

---

## Quick Start

### 1. Create a Server

Pick any of these providers. All you need is a cheap VPS with Docker.

---

<details>
<summary><b>Hetzner — $4.50/mo (best value)</b></summary>

1. Go to [console.hetzner.cloud](https://console.hetzner.cloud) and create an account
2. Click **Add Server**
3. Location: pick the nearest to you
4. Image: **Apps** tab → **Docker CE**
5. Type: **CX22** (2 vCPU, 4GB RAM) — $4.50/mo
6. SSH Keys: click **Add SSH Key**, paste your public key
   - Don't have one? On your laptop: `ssh-keygen -t ed25519`, then paste the contents of `~/.ssh/id_ed25519.pub`
7. Click **Create & Buy Now**
8. Copy the IP address shown

**Domain setup:** Go to your DNS provider. Add an A record pointing your domain (e.g. `tasks.yourdomain.com`) to the server IP.

</details>

---

<details>
<summary><b>Linode (Akamai) — $5/mo</b></summary>

1. Go to [cloud.linode.com](https://cloud.linode.com) and create an account
2. Click **Create Linode**
3. **Marketplace** tab → search **Docker** → select **Docker**
4. Region: pick the nearest to you
5. Plan: **Shared CPU** → **Nanode 1 GB** — $5/mo
6. Linode Label: `claude-task-runner`
7. Root Password: set one (but you'll use SSH keys)
8. SSH Keys: click **Add an SSH Key**, paste your public key
   - Don't have one? On your laptop: `ssh-keygen -t ed25519`, then paste the contents of `~/.ssh/id_ed25519.pub`
9. Click **Create Linode**
10. Wait for status **Running**, copy the IP address

**Domain setup:** Go to your DNS provider. Add an A record pointing your domain (e.g. `tasks.yourdomain.com`) to the server IP.

</details>

---

<details>
<summary><b>DigitalOcean — $4/mo</b></summary>

1. Go to [cloud.digitalocean.com](https://cloud.digitalocean.com) and create an account
2. Click **Create** → **Droplets**
3. **Marketplace** tab → search **Docker** → select **Docker on Ubuntu**
4. Region: pick the nearest to you
5. Size: **Basic** → **Regular** → **$4/mo** (512 MB / 1 CPU)
   - Or $6/mo for 1 GB if you want more headroom
6. Authentication: **SSH Key** → **New SSH Key**, paste your public key
   - Don't have one? On your laptop: `ssh-keygen -t ed25519`, then paste the contents of `~/.ssh/id_ed25519.pub`
7. Hostname: `claude-task-runner`
8. Click **Create Droplet**
9. Copy the IP address shown

**Domain setup:** Go to your DNS provider. Add an A record pointing your domain (e.g. `tasks.yourdomain.com`) to the server IP.

</details>

---

### 2. Run the Setup Wizard

On your laptop (not the server):

```bash
git clone <repo-url> claude-task-runner
cd claude-task-runner
./setup.sh
```

The wizard will walk you through:
- Server connection (IP, SSH user) — or show you how to create one
- Your domain name
- Claude authentication (Pro/Max subscription or API key)
- Users (names → auto-generates MCP API keys)
- Projects (name, directory on server, which users can access)

It generates config locally, pushes to the server via SSH, and starts the container.

**Using Claude Pro/Max subscription** (default): after deployment, the wizard will help you run `claude setup-token` on the server — a one-time step that authenticates Claude Code with your subscription. No API key needed.

**Using an API key**: the wizard asks for your key and includes it in the server config.

### 3. Connect to claude.ai

1. Go to **claude.ai** → **Settings** → **Connectors**
2. Click **Add MCP Server**
3. URL: `https://tasks.yourdomain.com/`
4. Auth type: **Bearer token**
5. Paste the API key the wizard printed for your user

Now in any claude.ai conversation, you can say *"submit a task to my-project to fix the login bug"* and it will queue it for execution.

---

## Updating Config

Re-run the wizard any time to add users, projects, or change settings:

```bash
./setup.sh
```

It remembers your server details and offers to keep existing users/projects.

---

## Architecture

```
claude.ai
  → MCP Server (server.py — API key auth, task submission)
    → Task Queue (filesystem: queued/ → processing/ → done/ | failed/)
      → Cron (every minute)
        → runner.py → claude -p "prompt" (headless, one-shot)
```

Everything runs in a single Docker container. Caddy runs alongside for auto-HTTPS.

**Why this works:**
- Simple and debuggable — markdown files in folders
- Battle-tested components — cron, filesystem, Docker
- `claude -p "prompt"` runs one-shot and exits — no daemon
- AI inference runs on Anthropic's servers — the VPS is just a script runner
- Every task is a readable file with full audit trail

---

## MCP Tools

| Tool | Description |
|------|-------------|
| `submit_task(project, prompt, timeout?)` | Queue a task. Returns task ID. |
| `task_status(task_id)` | Get status, timestamps, and prompt. |
| `list_tasks(status?, limit?)` | List recent tasks, filtered by status. |
| `cancel_task(task_id)` | Cancel a queued task. |
| `task_output(task_id)` | Get execution log for completed/failed task. |
| `list_projects()` | List projects you can access. |

---

## Task Lifecycle

```
queued → processing → done
                   → failed
                   → timeout
queued → cancelled (via cancel_task)
```

Each task is a markdown file with YAML frontmatter:

```markdown
---
id: 20260405_143022_a8f3
user: kris
project: my-project
status: queued
created_at: "2026-04-05T14:30:22Z"
max_timeout_minutes: 60
---

Fix the login bug in auth.py — users are getting
403 errors after password reset.
```

---

## Configuration Reference

### config/users.yaml

```yaml
users:
  kris:
    display_name: "Kris Randall"
    api_key: "ctr_a1b2c3..."  # auto-generated by setup wizard
```

### config/projects.yaml

```yaml
projects:
  my-project:
    directory: /root/apps/my-project
    description: "My web app"
    allowed_users:
      - kris
```

### Environment Variables (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | (optional) | Only needed if using API key auth instead of Claude subscription |
| `DOMAIN` | (required) | Your domain name |
| `SERVER_URL` | `https://{DOMAIN}` | Public URL for MCP auth metadata |
| `MAX_CONCURRENT` | `2` | Max simultaneous tasks |
| `DEFAULT_TIMEOUT_MINUTES` | `60` | Default task timeout |

---

## Operations

```bash
# SSH into your server
ssh root@your-server-ip

# View live logs
cd ~/claude-task-runner
docker compose logs -f

# Check the task queue
ls tasks/queued/       # pending
ls tasks/processing/   # running now
ls tasks/done/         # completed
ls tasks/failed/       # failed

# Read a task's output
cat logs/<task_id>.log

# Restart
docker compose restart

# Rebuild after code changes
docker compose up -d --build
```

---

## Security Model

1. **Access control** — user whitelist in `users.yaml`. API key per user. Unknown keys rejected.
2. **Project isolation** — users can only submit to projects in their `allowed_users` list.
3. **Network** — Caddy handles HTTPS automatically. Only ports 80, 443, and 22 are open.
4. **Secrets** — Claude credentials and config files have `600` permissions on server. Never stored in task files.
5. **Audit trail** — every task file preserved. Every execution logged. Nothing auto-deleted.
6. **Config lives on your laptop** — sensitive files are generated locally by the wizard and pushed via SSH. Re-run `./setup.sh` to update.

---

## What This Does NOT Do

- **No web UI** — use claude.ai or SSH
- **No automatic retries** — failed tasks stay failed; check logs, resubmit
- **No task dependencies** — each task is independent
- **No multi-machine** — single server
- **No billing tracking** — monitor via Anthropic dashboard

These are intentional. Simplicity is the design goal.

---

## License

MIT
