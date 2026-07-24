# AI Task Runner — container repo notes

Self-hosted system that accepts tasks via MCP, queues them as markdown files, and
runs them with the headless Claude Code CLI inside a Docker container. Core files:
`server.py` (MCP), `runner.py` (executor), `enqueue.py`, plus the `Dockerfile`,
`docker-compose.yml`, and `Caddyfile`.

## Branches

This repo carries three long-lived branches. Know which one you're on before you
build or deploy.

| Branch | Role |
|---|---|
| **`main`** | Clean open-source baseline. Just the task runner (Python + Node + Claude Code CLI). No project-specific toolchains. This is the branch to point people at and the one upstream/public users build from. |
| **`with-all-the-skills`** | The "kitchen sink" — every add-on accumulated over time, **including the full Godot 4.5 editor + Android export templates + a system-wide `godot` binary** and the game-build skills. Kept as history / reference. Heavy (~4 GB image). Not deployed. |
| **`slim`** | **The deployed branch.** `with-all-the-skills` minus the Godot editor/templates/keystore (see below), plus the JDK + Android SDK kept as shared infrastructure. This is what `ai-task-runner.com` builds and ships. |

`slim` is what the platform (`aitaskrunner_platform`) pins and builds via
`run.sh` / `container_refresh.sh`. If you're changing what actually runs in
production, you're changing `slim`.

Lineage: `main` → (`with-all-the-skills` = main + accumulated add-ons) →
(`slim` = with-all-the-skills, Godot removed, Android/JDK retained).

## Toolchain split: why Android/JDK are in the image but Godot is not

The image carries the **JDK 17 + Android SDK** but deliberately **not Godot**. The
seam follows release cadence and version-locking:

- **JDK + Android SDK live in the image.** They're slow-moving (~yearly), shared
  by any project on the runner, ~GBs, and not version-locked to Godot. Baking
  them in (with SDK licenses pre-accepted at build time) means a fresh container
  is self-sufficient — no per-task `sdkmanager` license dance.
- **Godot itself is pinned per-project, in-repo** (e.g. `lor/poc/.tools/`,
  gitignored) — *not* in the image. Godot ships every few months and its export
  templates must version-match the editor *exactly*. Pinning it next to the
  project makes a version bump a one-line script edit instead of an image
  rebuild. The Godot-version-locked part of Android support (the Gradle build
  template / `android_source.zip`) already ships inside the export templates, so
  the split stays clean: version-locked things in the repo, stable shared infra
  in the image.
- **JDK is pinned at 17 on purpose.** Godot 4.x's Gradle Android build expects
  JDK 17 and fails in unhelpful ways on newer JDKs. Do **not** "helpfully"
  upgrade it.
- **No system-wide `godot` binary.** One used to live at `/usr/local/bin/godot`
  and was removed on purpose — a bare `godot` would silently run the wrong
  version against a project's files. Tasks invoke the explicit in-repo path.
  Don't restore a global `godot`.

Android SDK versions in the image target Godot 4.7's export (per the official
4.7 Android export docs): SDK Platform 35, Build-Tools 35.0.1, Platform-Tools,
cmdline-tools (latest). NDK/CMake are omitted (only needed for native/GDExtension
builds); add them to the Dockerfile's SDK block if that changes.
