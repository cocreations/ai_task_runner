FROM python:3.12-slim-bookworm

# ---------------------------------------------------------------------------
# Base system deps: curl, git, Node.js 22.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg git \
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

# ---------------------------------------------------------------------------
# Playwright + headless Chromium + Pillow (for visual checks like
# moment-qa-gate's landing-page screenshots and the outreach home-screen
# preview). Pillow is REQUIRED, not optional: moment-agents' screenshot_app_home
# and web_preview use it for the pixel/blank detection (spinner vs real content)
# and the JPEG transcode. Without Pillow those checks silently no-op — the
# screenshot is captured blind with no verification — which shipped loading
# spinners as outreach hero images (moment-agents#13). Browsers go to a shared
# system path so both root and taskrunner can use them; --with-deps pulls in the
# apt system libraries Chromium needs (libglib-2.0, libnss3, libxkbcommon, etc.).
# ---------------------------------------------------------------------------
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN pip install --no-cache-dir playwright Pillow \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright

# ---------------------------------------------------------------------------
# moment-agents pipeline Python deps: Firestore state (firebase-admin), HTTP
# fetches (requests), and the pre-send MX/deliverability check (dnspython).
# Installed system-wide so every container user (root and taskrunner) can
# import them with no runtime `pip install`. These were previously pip-installed
# into ~/.local at task runtime, which a container recreate would silently wipe;
# baking them in makes a fresh container self-sufficient. Versions track
# moment-agents/requirements.txt.
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir "firebase-admin>=6.0.0" "requests>=2.28.0" "dnspython>=2.4.0"

# Copy application code
COPY server.py runner.py enqueue.py task_wait.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Config and data are mounted as volumes
VOLUME ["/app/config", "/app/tasks", "/app/logs", "/app/artifacts"]

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
