# norwegian-fours-video-generator

Build a single workout video for the **Norwegian 4×4** interval protocol from
a set of source clips. Terminal-only — edit a YAML config, run a script, get
an MP4. macOS-first; nothing GUI.

## What it produces

A 1920×1080 @ 30 fps MP4 with this structure:

```
intro
 └── warmup (recover clip #1)
 └── work clip #1   ─┐
 └── recover #2      │  repeats `intervals` times,
 └── work clip #2    │  with `recover` only between sets
 └── recover #3      │
 └── ...             │
 └── work clip #N   ─┘
 └── buffer (recover clip #N+1, fills time to total_minutes)
```

Defaults match the canonical 4×4 protocol: 4 min warmup, 4×(4 min hard +
3 min recovery), and an 11-min recovery buffer that totals 40 minutes.

A picture-in-picture countdown timer overlays each fragment in the top-left
corner. It hits `0:00` at the midpoint of the crossfade into the next
fragment, so period boundaries land at clean times (0:10, 4:10, 8:10, …).
On the buffer the countdown only runs for the first `recover_length`
seconds, then fades out.

## Install

Requires `python3` and `ffmpeg` on `PATH`:

```bash
brew install python ffmpeg
./bin/setup.sh
```

`setup.sh` creates `.venv` and installs `pyyaml` + `pillow`.

## Run

```bash
./bin/render.sh config.yaml -o output.mp4           # full 1080p
./bin/render.sh config.yaml -o output.mp4 --test    # 480x270 preview
```

The `--test` preview is ~1/4 the dimensions, lower fps, and ultrafast
encoding — use it to confirm the structure before kicking off a long render.

## Config

See `config.yaml`. The pieces you swap per session are:

- `work_clips` — exactly `intervals` entries (each `{ file, offset }`)
- `recover_clips` — exactly `intervals + 1` entries
  (warmup, between-set recoveries, final buffer)

The skeleton dials (`intervals`, `warmup_length`, `work_length`,
`recover_length`, `total_minutes`) default to the standard 4×4 protocol.
The buffer length is computed automatically.

The countdown overlay is rendered programmatically (no source video needed).
Tunable under the `countdown:` block: `font_size_frac`, `panel_alpha`,
`text_color`, `font_path`.

## Notes

- Inputs may be any codec/resolution ffmpeg can read; everything is normalized
  to 1080p with letterbox/pillarbox.
- Each fragment is encoded with a small pad on each side that gets consumed
  by the surrounding crossfades, so the cumulative timeline doesn't drift.
- Crossfades apply to both video (`xfade`) and audio (`acrossfade`).

## License

MIT
