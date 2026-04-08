#!/usr/bin/env bash
set -e

# Create task directories
mkdir -p /app/tasks/{queued,processing,done,failed} /app/logs

# Grant taskrunner user access to mounted volumes (claude -p runs as this user)
chown -R taskrunner:taskrunner /workspace /app/tasks /app/logs
chmod -R 775 /workspace /app/tasks /app/logs

# Start task runner loop in background (polls every 5 seconds)
(while true; do
    cd /app && python /app/runner.py >> /app/logs/runner.log 2>&1 || true
    sleep 5
done) &

echo "Task runner started — polling every 5s, MCP server starting on port 8080"

# Start MCP server in foreground
exec python /app/server.py
