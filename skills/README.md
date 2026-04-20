# Skills

Source of truth for Claude Code skills that the task runner injects into every
task prompt. `runner.py:load_skills()` reads SKILL.md files from
`{project_dir}/../.skills/`, so these need to be copied to the VPS at the
sibling location of your project directories.

## Deployment

Given projects at `/home/deploy/apps/moment`, `/home/deploy/apps/lor-elementals`,
etc., the skills directory is expected at `/home/deploy/apps/.skills/`.

Copy them up after running `./setup.sh`:

```
rsync -av --delete skills/ deploy@<SERVER>:/home/deploy/apps/.skills/
```

Or scp each one:

```
ssh deploy@<SERVER> mkdir -p /home/deploy/apps/.skills
scp -r skills/godot-apk-build skills/pixellab \
    deploy@<SERVER>:/home/deploy/apps/.skills/
```

The project directory itself (e.g. `/home/deploy/apps/lor-elementals`) and the
skills directory both need to be visible inside the task-runner container. Add
a `docker-compose.override.yml` next to `docker-compose.yml` with the bind
mounts — see `docker-compose.override.yml.example`.

## Current skills

- **godot-apk-build** — headless Android APK build for Legend of Rah.
- **pixellab** — pixel-art character/portrait generation via PixelLab.ai.
