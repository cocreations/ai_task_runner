#!/usr/bin/env bash
set -e

# Create task directories
mkdir -p /app/tasks/{queued,processing,done,failed} /app/logs /app/artifacts

# Grant taskrunner user access to mounted volumes (claude -p runs as this user)
chown -R taskrunner:taskrunner /workspace /app/tasks /app/logs /app/artifacts
chmod -R 775 /workspace /app/tasks /app/logs /app/artifacts

# Start task runner loop in background (polls every 5 seconds)
# Each invocation runs in background (&) so long-running tasks don't block the loop.
# Concurrency is controlled by MAX_CONCURRENT check inside runner.py.
(while true; do
    cd /app && python /app/runner.py >> /app/logs/runner.log 2>&1 &
    sleep 5
done) &

echo "Task runner started — polling every 5s, MCP server starting on port 8080"

# Start MCP server in foreground
exec python /app/server.py
