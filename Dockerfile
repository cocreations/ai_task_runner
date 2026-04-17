FROM python:3.12-slim

# Install system deps: curl, Node.js 22
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user for running Claude Code (--dangerously-skip-permissions requires non-root)
RUN useradd -m -s /bin/bash taskrunner

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server.py runner.py enqueue.py task_wait.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Config and data are mounted as volumes
VOLUME ["/app/config", "/app/tasks", "/app/logs"]

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
