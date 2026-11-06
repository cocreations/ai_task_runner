# What a task cannot change: isolation inside a container

Every task runs as the `taskrunner` user inside the account's container, with
a shell. Tasks on one project share the checkout, the user and the container,
and several run at once. This page says what the runner guarantees between
tasks, what it does not, and the one switch an untrusted project needs.

## The threat this is for

A project whose tasks act on **untrusted input** — a customer's website that an
agent reads and builds from, a form anyone on the internet can submit — can be
steered by that input ("prompt injection"). The agent then does what the page
says, with everything the container holds. The container's own contents are
chosen for that (no admin credentials; a read-only GitHub token; one scoped
token per task). What remains is **persistence**: could one steered task leave
something behind that runs in the *next* task, which belongs to someone else?

Before 2026-11-07 it could, in five places. All five are closed:

| Where a task could plant something | What would have run it | Now |
| --- | --- | --- |
| `~/.claude/settings.json` (hooks), `~/.claude/CLAUDE.md`, `~/.claude/skills`, `commands`, `agents`, `plugins` | Claude Code, at the start of every later task | Root-owned, read-only; the home and `~/.claude` are sticky (`1777`) so `taskrunner` can create its own files but not replace these (`entrypoint.sh`) |
| `~/.bashrc`, `~/.profile`, `~/.bash_profile`, `~/.bash_login` | Every Bash call of every later task | Root-owned, read-only |
| `~/.gitconfig`, `~/.config/git/config` (a `credential.helper` or `core.hooksPath` is a command) | The runner's own `git pull` before the next task | Root-owned, read-only; written by root at start |
| The checkout: a modified tracked file, an untracked `pipeline/json.py`, `CLAUDE.local.md`, `.claude/settings.local.json`, `.mcp.json` | Every later task on the project | `reset_before_task` (below) |
| `.mcp.json` in the checkout or `mcpServers` in `~/.claude.json` (an MCP "server" is any command) | Claude Code, at start | `--strict-mcp-config` on every run, with an empty server list when the project has no MCP file (`claude_command`) |

Two more things a task could do are closed at the same time:

- **Publish a page under the account's domain.** The platform serves each
  project's `.public/<project>` folder at a public URL. For a
  `reset_before_task` project the runner makes that folder root-owned and
  read-only before each task (`lock_public_dir`).
- **Read a stale, more powerful GitHub token.** The container's `GH_TOKEN`
  environment variable is frozen when the container is created; after the user
  saves a new token (`/secrets/github_token`) the old one was still in every
  task's environment. The task now gets the current one (`current_github_token`).

## `reset_before_task`

```yaml
projects:
  moment-agents:
    directory: /workspace/moment-agents
    pull_before_task: true
    reset_before_task: true     # tasks act on untrusted input
```

Before each task the runner, inside the pull lock:

1. deletes `CLAUDE.local.md`, `.claude/settings.local.json` and `.mcp.json`
   (git may ignore them, so `git clean` would not);
2. `git reset --hard` — tracked files back to the commit;
3. `git clean -fd` — untracked files and directories removed. **Ignored** paths
   stay (a running task's scratch lives there — `scraping-sites/`, build
   output) and so does the runner's `.ai-attachments/`;
4. then the usual `git pull --ff-only`.

The task's log says what was undone ("the checkout was reset first (restored
pipeline/moment_db.py; removed pipeline/json.py; deleted CLAUDE.local.md)").
Without the switch a changed tree still makes the task **fail** rather than
run — the right behaviour for a developer's project, where the changes are
someone's work.

Set it on the platform's Projects page ("Reset the checkout before each task")
or in `projects.yaml`. It implies a pull.

**Rule for skills on such a project:** anything a task keeps between its own
steps goes in a git-ignored directory. An untracked file in a tracked
directory is removed by the next task's reset — while this task may still be
running.

## What is NOT isolated (know this before you rely on it)

- **Tasks running at the same time, as the same user.** A steered task can
  read the task files in `/app/tasks/processing` (the prompts of the other
  running tasks, with whatever tokens they carry), their session files and
  their scratch. Keep per-task secrets short-lived and scoped to one thing.
- **Finished task files.** `/app/tasks/done/<id>.md` keeps the prompt. A token
  in a prompt is readable for as long as it is valid.
- **The container's own secrets.** Whatever is in the environment
  (`CLAUDE_CODE_OAUTH_TOKEN`, project env vars) is readable by every task.
  Put nothing in a container you would not give to the input it processes.
- **The network.** A task can reach the internet, and — unless the host
  blocks it — other containers on the same Docker bridge and the cloud
  metadata address. The platform's host blocks both
  (`aitaskrunner_platform/ops/host/`).
