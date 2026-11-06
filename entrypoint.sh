#!/usr/bin/env bash
set -e

# Create task directories
mkdir -p /app/tasks/{queued,processing,done,failed} /app/logs /app/artifacts

# Grant taskrunner user access to mounted volumes (claude -p runs as this user)
chown -R taskrunner:taskrunner /workspace /app/tasks /app/logs /app/artifacts
chmod -R 775 /workspace /app/tasks /app/logs /app/artifacts

# Configure git for the taskrunner user. Tasks run as `taskrunner` so all
# global git config has to live in /home/taskrunner.
#
# The GitHub token is read from /secrets/github_token (bind-mounted, updatable
# at any time) in preference to $GH_TOKEN — env vars are frozen at
# `docker create`, so a token saved after container creation never reaches the
# env. The runner also re-reads this file before every task, so a new token
# takes effect without any restart.
if [ -f /secrets/github_token ]; then
    GH_TOKEN="$(cat /secrets/github_token)"
fi
if [ -n "$GH_TOKEN" ]; then
    printf 'https://x-access-token:%s@github.com\n' "$GH_TOKEN" > /home/taskrunner/.git-credentials
    chown taskrunner:taskrunner /home/taskrunner/.git-credentials
    chmod 600 /home/taskrunner/.git-credentials
fi
# Always enable the store helper (harmless with no credentials file) so a
# token added later — via /secrets/github_token — works without reconfiguring.
# Written by root into taskrunner's home: the file is root-owned below, so a
# task cannot change it (a credential helper or hooksPath set by one task would
# otherwise run in every later task).
export HOME=/home/taskrunner
git config --global credential.helper store
git config --global --replace-all safe.directory '*'
if [ -n "$GIT_AUTHOR_NAME" ]; then
    git config --global user.name "${GIT_AUTHOR_NAME}"
fi
if [ -n "$GIT_AUTHOR_EMAIL" ]; then
    git config --global user.email "${GIT_AUTHOR_EMAIL}"
fi
export HOME=/root

# ── Nothing a task does may reach the next task (docs/isolation.md) ──────────
# claude runs as taskrunner, and taskrunner owned its whole home: a task could
# write ~/.claude/settings.json (hooks), ~/.claude/CLAUDE.md, ~/.bashrc, a skill
# or a git config that every later task — another customer's — would load.
# The home and ~/.claude stay writable for what Claude Code must write
# (sessions, projects, ~/.claude.json) but are root-owned with the sticky bit,
# and every file Claude Code or bash reads for INSTRUCTIONS is root-owned, so
# taskrunner can neither change nor replace it. Reset at every start, so a
# restart also discards anything planted before this was in place.
H=/home/taskrunner
echo '{}' > "$H/.claude/settings.json.new" 2>/dev/null || mkdir -p "$H/.claude"
rm -f "$H/.claude/settings.json.new"
rm -rf "$H/.claude/skills" "$H/.claude/commands" "$H/.claude/agents" "$H/.claude/plugins" \
       "$H/.config/git" "$H/.local/bin"
mkdir -p "$H/.claude/skills" "$H/.claude/commands" "$H/.claude/agents" "$H/.claude/plugins" \
         "$H/.config/git" "$H/.local/bin"
echo '{}' > "$H/.claude/settings.json"
: > "$H/.claude/CLAUDE.md"
: > "$H/.config/git/config"
for f in .bashrc .profile .bash_logout .bash_profile .bash_login .gitconfig; do
    [ -e "$H/$f" ] || : > "$H/$f"
done
chown root:root "$H" "$H/.claude" "$H/.config" "$H/.local" \
    "$H/.claude/settings.json" "$H/.claude/CLAUDE.md" "$H/.config/git/config" \
    "$H/.bashrc" "$H/.profile" "$H/.bash_logout" "$H/.bash_profile" "$H/.bash_login" "$H/.gitconfig"
chown -R root:root "$H/.claude/skills" "$H/.claude/commands" "$H/.claude/agents" \
    "$H/.claude/plugins" "$H/.config/git" "$H/.local/bin"
chmod 1777 "$H" "$H/.claude" "$H/.config" "$H/.local"
chmod 644 "$H/.claude/settings.json" "$H/.claude/CLAUDE.md" "$H/.config/git/config" \
    "$H/.bashrc" "$H/.profile" "$H/.bash_logout" "$H/.bash_profile" "$H/.bash_login" "$H/.gitconfig"
chmod 755 "$H/.claude/skills" "$H/.claude/commands" "$H/.claude/agents" "$H/.claude/plugins" \
    "$H/.config/git" "$H/.local/bin"
# The platform's skill files and secrets beside the projects are read by the
# runner (root) and by tasks, never written by tasks.
for d in /workspace/.skills /workspace/.secrets; do
    [ -d "$d" ] && chown -R root:root "$d" && chmod -R go-w "$d"
done

# Start task runner loop in background (polls every 0.5s)
# Each invocation runs in background (&) so long-running tasks don't block the loop.
# Concurrency is controlled by MAX_CONCURRENT check inside runner.py.
(while true; do
    cd /app && python /app/runner.py >> /app/logs/runner.log 2>&1 &
    sleep 0.5
done) &

echo "Task runner started — polling every 0.5s, MCP server starting on port 8080"

# Start MCP server in foreground
exec python /app/server.py
