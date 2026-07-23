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
su -s /bin/bash taskrunner -c "git config --global credential.helper store"
su -s /bin/bash taskrunner -c "git config --global --add safe.directory '*'"
if [ -n "$GIT_AUTHOR_NAME" ]; then
    su -s /bin/bash taskrunner -c "git config --global user.name '${GIT_AUTHOR_NAME}'"
fi
if [ -n "$GIT_AUTHOR_EMAIL" ]; then
    su -s /bin/bash taskrunner -c "git config --global user.email '${GIT_AUTHOR_EMAIL}'"
fi

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
