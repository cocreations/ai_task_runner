## Skill: godot-apk-build

Builds a signed Android APK for the Legend of Rah: Elementals Godot project
and publishes it to a public URL that can be tapped on a phone to install.

### When to use

Invoke this at the end of any task where the user asks for a testable build,
an APK, something to install on their phone, or phrases like "send me a build",
"make an APK", "ship it to my phone". If the task only modifies code without
requesting a build, do not run it — just report the code changes.

### How to use

From the project root (`/home/deploy/apps/lor-elementals` inside the container),
run:

```
./scripts/build/build_apk.sh
```

All required environment variables (`LOR_KEYSTORE_PATH`, `LOR_KEYSTORE_PASSWORD`,
`LOR_KEY_ALIAS`, `LOR_KEY_PASSWORD`, `ARTIFACT_DIR`, `ARTIFACT_BASE_URL`) are
already in the process environment — you do not need to set them.

The script takes 2–5 minutes. It:
1. Patches `export_presets.cfg` in place with container-valid paths and the
   keystore fields (restored from git on exit).
2. Runs `godot --headless --export-release "Android"` to produce an APK.
3. Runs `zipalign` and `apksigner sign` on it.
4. Copies the final APK to `$ARTIFACT_DIR/lor-<timestamp>.apk`.
5. Prints a line: `URL: https://<domain>/artifacts/lor-<timestamp>.apk`.

### What to include in your final task-result summary

The final line of your result summary MUST be the APK URL on its own line,
formatted like:

```
APK: https://<domain>/artifacts/lor-<timestamp>.apk
```

Copy the URL exactly as the build script printed it. The user will tap this
link on their phone to install and test the build, so do not wrap it in
backticks or markdown — just the bare URL on its own line.

If the build fails, report the error clearly and do not fabricate a URL.
