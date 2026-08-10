# AI Task Runner — container repo notes

Self-hosted system that accepts tasks via MCP, queues them as markdown files, and
runs them with the headless Claude Code CLI inside a Docker container. Core files:
`server.py` (MCP), `runner.py` (executor), `enqueue.py`, plus the `Dockerfile`,
`docker-compose.yml`, and `Caddyfile`.

## Branches

This repo carries three long-lived branches:

| Branch | Role |
|---|---|
| **`main`** | Clean open-source baseline (this branch). Just the task runner — Python + Node + Claude Code CLI, no project-specific toolchains. Build from here for a vanilla self-hosted runner. |
| **`with-all-the-skills`** | The "kitchen sink" — every add-on accumulated over time, including the full Godot editor + Android export templates + a system-wide `godot` binary and game-build skills. Heavy (~4 GB image). Kept as history; not deployed. |
| **`slim`** | The deployed branch. `with-all-the-skills` with the Godot editor/templates removed but the JDK + Android SDK retained as shared build infrastructure. This is what the hosted platform builds and ships. |

Lineage: `main` → (`with-all-the-skills` = main + accumulated add-ons) →
(`slim` = with-all-the-skills, Godot removed, Android/JDK retained).

The deployment-specific toolchain and its rationale (why the JDK + Android SDK
live in the image while Godot is pinned per-project instead) are documented on
the **`slim`** branch's `CLAUDE.md`.
