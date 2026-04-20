## Skill: pixellab

Generates pixel-art character sprites, rotations, walk animations, and
conversation portraits for Legend of Rah: Elementals via the PixelLab.ai API.

### When to use

Invoke when the user asks to create a new character, NPC, sprite, portrait,
or animation — e.g. "make a new Ancient Wisp variant", "generate a portrait
for the Angel character", "create walk animations for the star wisp".

Do NOT use for non-pixel-art image work, or for game logic that doesn't need
new art.

### Credentials

`PIXELLAB_API_KEY` is already set in the process environment. Do not ask the
user for it, do not attempt to read `.env` — just use the env var directly.
If the variable is empty, report that and stop.

### Reference implementation

The project has a working example at
`scripts/tools/generate_wisp.py`. Read it first — it contains the canonical
API call pattern, prompt conventions, and post-processing steps for this
codebase. When generating new characters, model your script on it rather
than writing a new pattern from scratch.

### API shape (PixelLab v1)

- Base URL: `https://api.pixellab.ai/v1`
- Auth: `Authorization: Bearer $PIXELLAB_API_KEY`
- Endpoints used in this project:
  - `/generate-image-pixflux` — base sprite generation (64×64)
  - `/rotate` — derive 4-direction rotations from a base sprite
  - `/animate-with-text` — per-direction walk animation frames
- Use **pixflux**, not bitforge — bitforge produces garbled output at 64×64.
- `animate-with-text` is capped at 4 frames and only supports 64×64.

### Project conventions

- Sprites are **64×64**, `no_background: true`, `direction: "south"`,
  `view: "low top-down"`.
- Use `color_image` set to an existing character sprite (usually
  `assets/animations/player/rotations/south.png` — Rah) to keep the palette
  consistent.
- NPCs only need **4 cardinal directions** (south, east, west, north).
  Do not generate diagonal frames for NPCs — the wander AI only moves
  cardinally and the animation controller never requests them.
- Walk animations: 4 frames per direction. East and west are the most prone
  to distortion — generate 10+ seeds per direction and pick visually.
- After generating walk frames, run colour correction: shift average RGB to
  match the idle sprite, then quantize to the idle sprite's palette with
  PIL's `quantize(palette=...)`. API walk frames drift lighter than idle
  sprites otherwise.
- Portraits: generated at 128×128 and upscaled 4× nearest-neighbour to
  512×512. Use `color_image` with `assets/icon.png` as style reference.
  Seed 123 historically produced the best open/closed-mouth pair for Rah.

### Output placement

- Sprite PNGs → `assets/animations/{character_name}/`
- Portrait PNGs → `assets/portraits/`
- SpriteFrames `.tres` → `resources/`
- Scene file → `scenes/characters/{name}_character.tscn`

### Task-result reporting

When finished, list the generated files and any seed numbers used, so the
user can reproduce or regenerate individual variants later. Do not commit
the new assets unless the user asked for a commit.
