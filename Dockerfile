FROM python:3.12-slim-bookworm

# ---------------------------------------------------------------------------
# Base system deps: curl, git, Node.js 22, plus Godot/Android toolchain needs.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg git \
    unzip zip wget \
    openjdk-17-jdk-headless \
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
# Godot 4.5.1-stable headless + Android export templates.
# ---------------------------------------------------------------------------
ARG GODOT_VERSION=4.5.1-stable
ENV GODOT_VERSION=${GODOT_VERSION}

RUN set -eux; \
    mkdir -p /opt/godot; \
    curl -fsSL "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}/Godot_v${GODOT_VERSION}_linux.x86_64.zip" \
        -o /tmp/godot.zip; \
    unzip -q /tmp/godot.zip -d /opt/godot; \
    mv "/opt/godot/Godot_v${GODOT_VERSION}_linux.x86_64" /opt/godot/godot; \
    chmod +x /opt/godot/godot; \
    ln -s /opt/godot/godot /usr/local/bin/godot; \
    rm /tmp/godot.zip

RUN set -eux; \
    curl -fsSL "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}/Godot_v${GODOT_VERSION}_export_templates.tpz" \
        -o /tmp/templates.tpz; \
    TEMPLATES_VERSION="$(echo "${GODOT_VERSION}" | tr '-' '.')"; \
    mkdir -p "/root/.local/share/godot/export_templates/${TEMPLATES_VERSION}"; \
    mkdir -p "/home/taskrunner/.local/share/godot/export_templates/${TEMPLATES_VERSION}"; \
    unzip -q /tmp/templates.tpz -d /tmp/templates; \
    cp -r /tmp/templates/templates/. "/root/.local/share/godot/export_templates/${TEMPLATES_VERSION}/"; \
    cp -r /tmp/templates/templates/. "/home/taskrunner/.local/share/godot/export_templates/${TEMPLATES_VERSION}/"; \
    chown -R taskrunner:taskrunner /home/taskrunner/.local; \
    rm -rf /tmp/templates /tmp/templates.tpz

# ---------------------------------------------------------------------------
# Android SDK: cmdline-tools, platform-tools, platform 33, build-tools 34.0.0.
# ---------------------------------------------------------------------------
ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH=$PATH:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/build-tools/34.0.0

ARG ANDROID_CMDLINE_TOOLS_VERSION=11076708
RUN set -eux; \
    mkdir -p "${ANDROID_HOME}/cmdline-tools"; \
    curl -fsSL "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_CMDLINE_TOOLS_VERSION}_latest.zip" \
        -o /tmp/cmdtools.zip; \
    unzip -q /tmp/cmdtools.zip -d /tmp/cmdtools; \
    mv /tmp/cmdtools/cmdline-tools "${ANDROID_HOME}/cmdline-tools/latest"; \
    rm -rf /tmp/cmdtools /tmp/cmdtools.zip; \
    yes | sdkmanager --licenses > /dev/null; \
    sdkmanager --install \
        "platform-tools" \
        "platforms;android-33" \
        "platforms;android-34" \
        "build-tools;34.0.0" > /dev/null; \
    chown -R taskrunner:taskrunner "${ANDROID_HOME}"

# ---------------------------------------------------------------------------
# Godot editor settings — tell Godot where to find SDK/JDK for Android export.
# Written for both root (in case tasks run as root) and taskrunner.
# A debug keystore is generated so Godot doesn't refuse to export.
# ---------------------------------------------------------------------------
RUN set -eux; \
    keytool -genkey -v -keystore /opt/godot/debug.keystore \
        -alias androiddebugkey -dname "CN=Android Debug,O=Android,C=US" \
        -storepass android -keypass android -keyalg RSA -keysize 2048 -validity 10000; \
    chmod 644 /opt/godot/debug.keystore

COPY godot-editor-settings-4.tres /tmp/editor_settings-4.tres
RUN set -eux; \
    for HOME_DIR in /root /home/taskrunner; do \
        mkdir -p "${HOME_DIR}/.config/godot"; \
        cp /tmp/editor_settings-4.tres "${HOME_DIR}/.config/godot/editor_settings-4.tres"; \
    done; \
    chown -R taskrunner:taskrunner /home/taskrunner/.config; \
    rm /tmp/editor_settings-4.tres

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Playwright + headless Chromium (for visual checks like moment-qa-gate's
# landing-page screenshots). Browsers installed to a shared system path so
# both root and taskrunner can use them. --with-deps pulls in the apt
# system libraries Chromium needs (libglib-2.0, libnss3, libxkbcommon, etc.).
# ---------------------------------------------------------------------------
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN pip install --no-cache-dir playwright \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright

# Copy application code
COPY server.py runner.py enqueue.py task_wait.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Config and data are mounted as volumes
VOLUME ["/app/config", "/app/tasks", "/app/logs", "/app/artifacts"]

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
