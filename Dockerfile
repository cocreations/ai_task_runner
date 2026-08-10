FROM python:3.12-slim-bookworm

# ---------------------------------------------------------------------------
# Base system deps: curl, git, Node.js 22, plus unzip + JDK 17 for the
# Android SDK below. JDK is pinned to 17 ON PURPOSE — Godot 4.x's Gradle
# Android build expects 17 and fails in unhelpful ways on newer JDKs. Do NOT
# "helpfully" bump this.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg git \
    unzip openjdk-17-jdk-headless \
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

# ---------------------------------------------------------------------------
# JDK 17 + Android SDK for headless Android exports (e.g. the lor Godot PoC).
#
# WHY this lives in the image while Godot itself does NOT: the JDK + Android
# SDK are slow-moving (~yearly), shared by every project on the runner, ~GBs,
# and NOT version-locked to any Godot release. The Godot editor + export
# templates ship every few months and the templates must version-match the
# editor exactly, so they're pinned PER-PROJECT in-repo (e.g. lor/poc/.tools/),
# not baked here — a Godot bump stays a one-line project edit, not an image
# rebuild. (The Godot-version-locked part of Android support — the Gradle build
# template / android_source.zip — already ships inside the export templates.)
#
# Versions target Godot 4.7's Android export (per the official 4.7 export docs):
# SDK Platform 35, Build-Tools 35.0.1, Platform-Tools, cmdline-tools (latest).
# NDK/CMake are intentionally omitted — only standard GDScript exports are in
# scope; add "ndk;..."/"cmake;..." here if native/GDExtension builds start.
#
# There is deliberately NO system-wide `godot` binary (one used to live at
# /usr/local/bin/godot). It was a footgun: a bare `godot` would silently run the
# wrong version against a project's files. Tasks invoke the explicit in-repo
# path instead. Do NOT restore a global godot here.
# ---------------------------------------------------------------------------
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH=$PATH:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools

# Bootstrap the command-line tools from /tmp, then use them to install the
# current cmdline-tools;latest plus the pinned build packages into ANDROID_HOME.
# Bootstrapping outside ANDROID_HOME avoids sdkmanager updating the binary it is
# itself running. Licenses are accepted at build time so no task ever has to.
ARG ANDROID_CMDLINE_TOOLS_VERSION=11076708
RUN set -eux; \
    mkdir -p "${ANDROID_HOME}"; \
    curl -fsSL "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_CMDLINE_TOOLS_VERSION}_latest.zip" \
        -o /tmp/cmdtools.zip; \
    unzip -q /tmp/cmdtools.zip -d /tmp/cmdtools-boot; \
    yes | /tmp/cmdtools-boot/cmdline-tools/bin/sdkmanager --sdk_root="${ANDROID_HOME}" --licenses > /dev/null; \
    /tmp/cmdtools-boot/cmdline-tools/bin/sdkmanager --sdk_root="${ANDROID_HOME}" \
        "cmdline-tools;latest" \
        "platform-tools" \
        "platforms;android-35" \
        "build-tools;35.0.1" > /dev/null; \
    rm -rf /tmp/cmdtools.zip /tmp/cmdtools-boot; \
    chown -R taskrunner:taskrunner "${ANDROID_HOME}"

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
COPY server.py runner.py housekeeping.py enqueue.py task_wait.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Config and data are mounted as volumes
VOLUME ["/app/config", "/app/tasks", "/app/logs", "/app/artifacts"]

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
