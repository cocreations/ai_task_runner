#!/usr/bin/env bash
set -e

# Create task directories
mkdir -p /app/tasks/{queued,processing,done,failed} /app/logs

# Write cron job for runner
echo "* * * * * cd /app && /usr/local/bin/python /app/runner.py >> /app/logs/runner.log 2>&1" > /etc/cron.d/task-runner
chmod 0644 /etc/cron.d/task-runner
crontab /etc/cron.d/task-runner

# Pass environment to cron jobs
printenv | grep -vE '^(HOME|USER|LOGNAME|PATH|SHELL|TERM|HOSTNAME|PWD|SHLVL|_)=' > /etc/environment

# Start cron in background
cron

echo "Task runner started — cron active, MCP server starting on port 8080"

# Start MCP server in foreground
exec python /app/server.py
