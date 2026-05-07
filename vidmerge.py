#!/usr/bin/env python3
"""vidmerge — concatenate videos with normalization, crossfades, intro, and PiP countdown.

Usage: ./vidmerge.py config.yaml -o output.mp4
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

ROUNDED_FONT_CANDIDATES = [
    "/System/Library/Fonts/SFCompactRounded.ttf",
    "/System/Library/Fonts/SFNSRounded.ttf",
    "/Library/Fonts/Arial Rounded Bold.ttf",
]

TARGET_W = 1920
TARGET_H = 1080
TARGET_FPS = 30
SAMPLE_RATE = 48000
DEFAULT_FONT = "/System/Library/Fonts/Helvetica.ttc"
ENCODE_PRESET = "veryslow"
ENCODE_CRF = "17"

# 4xN skeleton constants (the "Norwegian Fours" protocol).
INTRO_LENGTH = 10           # seconds of title card before warmup
TRANSITION = 2.0            # crossfade duration (s) between every pair of parts
WARMUP_LENGTH = 240         # warmup recover-clip duration
WORK_LENGTH = 240           # each hard interval
RECOVER_LENGTH = 180        # each between-sets recovery + buffer countdown
BUFFER_FADEOUT = 1.0        # PiP alpha fade-out at end of buffer countdown

# Picture-in-picture countdown styling.
PIP_MARGIN = 0              # top-left flush
PIP_H_PADDING_FRAC = 0.15   # horizontal padding inside the panel

# Countdown look.
COUNTDOWN_FONT_SIZE_FRAC = 0.8
COUNTDOWN_PANEL_ALPHA = 0.55
COUNTDOWN_TEXT_COLOR = "white"


def apply_test_mode():
    """Shrink output to 480x270 (1/4 of FullHD per side) / 15fps / ultrafast
    for fast structural previews. Same 16:9 aspect as the final output.
    """
    global TARGET_W, TARGET_H, TARGET_FPS, ENCODE_PRESET, ENCODE_CRF
    TARGET_W, TARGET_H, TARGET_FPS = 480, 270, 15
    ENCODE_PRESET, ENCODE_CRF = "ultrafast", "30"


def run(cmd):
    cmd = [str(c) for c in cmd]
    print("+", " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, check=True)


def probe_duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]).decode().strip()
    return float(out)


def probe_dimensions(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        str(path),
    ]).decode().strip()
    w, h = out.split(",")
    return int(w), int(h)


def has_audio(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]).decode().strip()
    return "audio" in out


def normalize_fragment(src, offset, length, countdown, pip, out,
                       countdown_length=None, countdown_fadeout=1.0,
                       countdown_align="end",
                       transition=2.0, is_first=False, is_last=False):
    """Cut from src, fit-to-1080p, overlay countdown PiP.

    Each fragment is encoded with τ/2 seconds of padding on each side that
    isn't an outer boundary, so that crossfades centered on period boundaries
    don't shift the cumulative timeline. The source is read from
    [offset - lead_pad, offset + length + tail_pad] (clamped to source bounds).

    countdown_length: seconds of PiP to show (default = full fragment length).
    countdown_fadeout: seconds of alpha fade-out at the end of the PiP.
    countdown_align: 'end' = 0:00 lands at the midpoint of the crossfade into
        the next fragment (default). 'start' = countdown begins at fragment
        start and runs for countdown_length seconds, then fades out.
    transition: crossfade duration into the next fragment.
    is_first: true for the very first encoded part (no leading pad needed).
    is_last: true for the very last encoded part (no trailing pad needed).
    """
    half = transition / 2.0
    lead_pad = 0.0 if is_first else half
    tail_pad = 0.0 if is_last else half

    src_dur = probe_duration(src)
    # Pull lead_pad from before offset when possible, otherwise extend with
    # cloned frames at the start so the fragment's encoded duration is always
    # length + lead_pad + tail_pad.
    src_offset = max(0.0, offset - lead_pad)
    src_lead = offset - src_offset            # actually pulled from before offset
    clone_lead = lead_pad - src_lead          # filled with held first frame
    src_take_no_tail = src_lead + length      # source content actually consumed
    if src_offset + src_take_no_tail + tail_pad > src_dur + 0.05:
        sys.exit(
            f"ERROR: {src} is {src_dur:.2f}s, can't take "
            f"[{src_offset:.2f},{src_offset + src_take_no_tail + tail_pad:.2f}]"
        )
    src_take = src_take_no_tail + tail_pad
    encoded_dur = lead_pad + length + tail_pad
    real_start = lead_pad
    real_end = real_start + length

    cd_total = probe_duration(countdown)
    if countdown_length is None:
        countdown_length = length
    if countdown_length > cd_total + 0.05:
        sys.exit(f"ERROR: countdown_length {countdown_length}s exceeds countdown duration {cd_total:.2f}s")
    if countdown_length > length + 0.05:
        sys.exit(f"ERROR: countdown_length {countdown_length}s exceeds fragment length {length}s")
    cd_visible = float(pip.get("countdown_visible_duration", cd_total))
    # PiP placement is in fragment-internal time, where the "real" period
    # occupies [real_start, real_end]. The crossfade into the next fragment
    # is centered at real_end (its midpoint = real_end), spanning
    # [real_end - transition/2, real_end + transition/2].
    if countdown_align == "start":
        pip_start = real_start
        pip_end = min(real_end, real_start + countdown_length)
    elif countdown_align == "end":
        pip_end = real_end
        pip_start = max(real_start, pip_end - countdown_length)
    else:
        sys.exit(f"ERROR: unknown countdown_align {countdown_align!r} (expected 'start' or 'end')")
    visible_len = pip_end - pip_start                          # actually shown
    cd_start = cd_visible - visible_len                        # source-side seek
    if cd_start < -0.05:
        sys.exit(f"ERROR: countdown_visible_duration too small for countdown_length {countdown_length}")
    fade = max(0.0, float(countdown_fadeout))
    fade_start_in_pip = max(0.0, visible_len - fade)            # fade-out within the pip stream

    margin = pip["margin"]

    # If we couldn't pull lead_pad from before offset, prepend cloned first
    # frame (and silent audio) so encoded_dur is always lead_pad+length+tail_pad.
    tpad_v = (
        f"tpad=start_duration={clone_lead}:start_mode=clone,"
        if clone_lead > 0 else ""
    )

    fc = [
        # Main video: optional clone-pad, then fit-to-1080p / normalize.
        f"[0:v]setpts=PTS-STARTPTS,{tpad_v}"
        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
        f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={TARGET_FPS},format=yuv420p[base]",

        # Countdown is already rendered at the right pixel dimensions and with
        # alpha. Apply alpha fade-out at the end of the PiP, then shift PTS so
        # the overlay starts at pip_start within the fragment.
        f"[1:v]setpts=PTS-STARTPTS,fps={TARGET_FPS},format=yuva420p,"
        f"fade=t=out:st={fade_start_in_pip}:d={fade}:alpha=1,"
        f"setpts=PTS+{pip_start}/TB[pip]",

        # PiP overlay top-left; only enabled within the countdown window.
        f"[base][pip]overlay={margin}:{margin}:"
        f"enable='between(t,{pip_start},{pip_end})':eof_action=pass[v]",
    ]

    apad_a = (
        f"adelay={int(clone_lead*1000)}|{int(clone_lead*1000)},"
        if clone_lead > 0 else ""
    )
    if has_audio(src):
        fc.append(
            f"[0:a]aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo,"
            f"{apad_a}apad,atrim=duration={encoded_dur},asetpts=PTS-STARTPTS[a]"
        )
        extra_in = []
        a_map = ["-map", "[a]"]
    else:
        extra_in = ["-f", "lavfi", "-t", str(encoded_dur), "-i",
                    f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
        a_map = ["-map", "2:a"]

    cmd = [
        "ffmpeg", "-y",
        "-ss", src_offset, "-t", src_take, "-i", src,
        "-ss", cd_start, "-t", visible_len, "-i", countdown,
        *extra_in,
        "-filter_complex", ";".join(fc),
        "-map", "[v]", *a_map,
        "-t", encoded_dur,
        "-c:v", "libx264", "-preset", ENCODE_PRESET, "-crf", ENCODE_CRF,
        "-c:a", "aac", "-b:a", "192k",
        "-r", TARGET_FPS, "-pix_fmt", "yuv420p",
        out,
    ]
    run(cmd)


def pick_rounded_font():
    for cand in ROUNDED_FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def _load_countdown_font(pip_h, cfg):
    font_path = cfg.get("font_path") or pick_rounded_font()
    font_size = max(8, int(pip_h * float(cfg.get("font_size_frac", 0.8))))
    try:
        return ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
    except OSError:
        return ImageFont.load_default()


def measure_countdown_text_width(pip_h, cfg):
    """Width of the widest M:SS string we'll render at the given panel height.
    The colon separator is the only non-digit char; widest digit varies by
    font, so we measure '8:88' as a heuristic upper bound.
    """
    font = _load_countdown_font(pip_h, cfg)
    bbox = font.getbbox("8:88")
    return bbox[2] - bbox[0]


def render_countdown_frame(seconds_left, size, cfg):
    """Return a PIL.Image (RGBA) showing M:SS for seconds_left at the given size.

    cfg keys (all optional, sensible defaults):
        font_path, font_size_frac, panel_alpha, text_color
    Vertical centering uses the cap-box of the digit "0" so the visible glyphs
    sit on the panel's vertical midline regardless of font metrics.
    """
    w, h = size
    panel_alpha = int(255 * float(cfg.get("panel_alpha", 0.55)))
    text_color = cfg.get("text_color", "white")
    font = _load_countdown_font(h, cfg)

    img = Image.new("RGBA", (w, h), (0, 0, 0, panel_alpha))
    draw = ImageDraw.Draw(img)

    text = f"{seconds_left // 60}:{seconds_left % 60:02d}"
    # Horizontal: use the actual inked width of THIS particular string.
    text_bbox = draw.textbbox((0, 0), text, font=font, anchor="lt")
    text_w = text_bbox[2] - text_bbox[0]
    x = (w - text_w) / 2 - text_bbox[0]

    # Vertical: with anchor="lt", the draw point's y is the top of the inked
    # region. For digits (no descenders, no diacritics) the inked height is
    # the cap height of "0". Center that on the panel midline.
    cap_bbox = font.getbbox("0")
    cap_h = cap_bbox[3] - cap_bbox[1]
    y = (h - cap_h) / 2

    draw.text((x, y), text, fill=text_color, font=font, anchor="lt")
    return img


def make_countdown_clip(max_seconds, size, cfg, workdir, out):
    """Render a transparent countdown clip from M:SS down to 0:00 at 1 fps.

    The clip has max_seconds + 1 frames covering [0, max_seconds + 1) seconds:
    frame at t=k shows M:SS for (max_seconds - k). So the last visible second
    (t in [max_seconds, max_seconds + 1)) shows 0:00, matching the convention
    used elsewhere ("0:00 lands at the end of the visible window").

    Output is ProRes 4444 .mov with alpha so the rest of the pipeline can
    overlay it directly. The per-fragment ffmpeg pass re-times it as needed.
    """
    frames_dir = workdir / "_countdown_frames"
    frames_dir.mkdir(exist_ok=True)
    total = max_seconds + 1
    for k in range(total):
        s = max_seconds - k  # max_seconds, max_seconds-1, ..., 1, 0
        render_countdown_frame(s, size, cfg).save(frames_dir / f"f_{k:05d}.png")
    cmd = [
        "ffmpeg", "-y",
        "-framerate", "1", "-i", str(frames_dir / "f_%05d.png"),
        "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
        "-vf", f"fps={TARGET_FPS}",
        str(out),
    ]
    run(cmd)


def render_title_png(title, font_path, out_path):
    """Render a TARGET_W x TARGET_H PNG with the title centered in white on black."""
    img = Image.new("RGB", (TARGET_W, TARGET_H), color="black")
    draw = ImageDraw.Draw(img)
    # Pick a font size that scales with output height.
    font_size = max(24, int(TARGET_H * 0.09))
    try:
        font = ImageFont.truetype(font_path, font_size)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), title, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (TARGET_W - tw) // 2 - bbox[0]
    y = (TARGET_H - th) // 2 - bbox[1]
    draw.text((x, y), title, fill="white", font=font)
    img.save(out_path)


def make_intro(title, length, font_path, workdir, out, countdown, pip,
               transition=2.0):
    """Generate a black-background intro clip with centered title text and a
    countdown PiP for the full intro duration (0:length -> 0:00).

    The intro is encoded with τ/2 of trailing pad so that the crossfade into
    the first fragment is centered on the period boundary T_1 = length.
    """
    title_png = workdir / "_intro_title.png"
    render_title_png(title, font_path, title_png)

    tail_pad = transition / 2.0
    encoded_dur = length + tail_pad

    cd_total = probe_duration(countdown)
    cd_visible = float(pip.get("countdown_visible_duration", cd_total))
    cd_start = cd_visible - length
    if cd_start < -0.05:
        sys.exit(f"ERROR: intro length {length}s exceeds countdown_visible_duration {cd_visible}s")

    margin = pip["margin"]
    # PiP is shown for the first `length` seconds (intro's "real" duration);
    # the trailing tail_pad has no overlay (the next fragment's xfade is
    # blending into it).
    fc = [
        f"[0:v]fps={TARGET_FPS},format=yuv420p[base]",
        f"[1:v]setpts=PTS-STARTPTS,fps={TARGET_FPS},format=yuva420p[pip]",
        f"[base][pip]overlay={margin}:{margin}:"
        f"enable='lt(t,{length})':eof_action=pass[v]",
    ]

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-t", encoded_dur, "-i", str(title_png),
        "-ss", cd_start, "-t", length, "-i", countdown,
        "-f", "lavfi", "-t", encoded_dur, "-i",
            f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
        "-filter_complex", ";".join(fc),
        "-map", "[v]", "-map", "2:a",
        "-t", encoded_dur,
        "-c:v", "libx264", "-preset", ENCODE_PRESET, "-crf", ENCODE_CRF,
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        out,
    ]
    run(cmd)


def crossfade_chain(parts, output, transition):
    """Chain xfade + acrossfade across N normalized clips."""
    if len(parts) == 1:
        shutil.copy(parts[0], output)
        return

    durs = [probe_duration(p) for p in parts]
    inputs = []
    for p in parts:
        inputs += ["-i", str(p)]

    v_chain, a_chain = [], []
    prev_v, prev_a = "[0:v]", "[0:a]"
    cum = durs[0]

    for i in range(1, len(parts)):
        offset = cum - transition
        last = i == len(parts) - 1
        v_out = "[vout]" if last else f"[v{i}]"
        a_out = "[aout]" if last else f"[a{i}]"
        v_chain.append(
            f"{prev_v}[{i}:v]xfade=transition=fade:"
            f"duration={transition}:offset={offset}{v_out}"
        )
        a_chain.append(f"{prev_a}[{i}:a]acrossfade=d={transition}{a_out}")
        prev_v, prev_a = v_out, a_out
        cum += durs[i] - transition

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", ";".join(v_chain + a_chain),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", ENCODE_PRESET, "-crf", ENCODE_CRF,
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        output,
    ]
    run(cmd)


def build_fragments(cfg):
    """Translate the 4xN skeleton config into an explicit fragment list.

    Layout: warmup (recover[0]) ->
            [work[i], recover[i+1]] for i in 0..intervals-2 ->
            work[intervals-1] ->
            buffer (recover[intervals])
    Total recover_clips needed: intervals + 1 (warmup, intervals-1 between sets, buffer).
    Total work_clips needed: intervals.
    Buffer length is whatever's needed to hit total_minutes.
    """
    intervals = int(cfg.get("intervals", 4))
    total_seconds = int(cfg.get("total_minutes", 40)) * 60

    work_clips = cfg.get("work_clips")
    recover_clips = cfg.get("recover_clips")
    if not work_clips or not recover_clips:
        sys.exit("ERROR: config must define `work_clips` and `recover_clips` lists.")
    if len(work_clips) != intervals:
        sys.exit(
            f"ERROR: need exactly {intervals} work_clips for intervals={intervals}, "
            f"got {len(work_clips)}."
        )
    needed_recover = intervals + 1
    if len(recover_clips) != needed_recover:
        sys.exit(
            f"ERROR: need exactly {needed_recover} recover_clips for intervals={intervals} "
            f"(warmup + {intervals - 1} between-sets + buffer), got {len(recover_clips)}."
        )

    used = WARMUP_LENGTH + intervals * WORK_LENGTH + (intervals - 1) * RECOVER_LENGTH
    buffer_length = total_seconds - used
    if buffer_length < RECOVER_LENGTH:
        sys.exit(
            f"ERROR: total_minutes={total_seconds // 60} too short for "
            f"{intervals}x{WORK_LENGTH // 60}+{RECOVER_LENGTH // 60} skeleton "
            f"(need ≥ {(used + RECOVER_LENGTH) // 60} min)."
        )

    def slot(clip, length, **extra):
        return {
            "file": clip["file"],
            "offset": int(clip.get("offset", 0)),
            "length": length,
            **extra,
        }

    frags = [slot(recover_clips[0], WARMUP_LENGTH)]
    for i in range(intervals):
        frags.append(slot(work_clips[i], WORK_LENGTH))
        if i < intervals - 1:
            frags.append(slot(recover_clips[i + 1], RECOVER_LENGTH))
    frags.append(slot(
        recover_clips[intervals],
        buffer_length,
        countdown_length=RECOVER_LENGTH,
        countdown_fadeout=BUFFER_FADEOUT,
        countdown_align="start",
    ))
    return frags


def validate_fragments(fragments, countdown, pip):
    """Verify each clip is long enough and PiP/countdown sizing is consistent.
    Bails before any encoding starts so we don't waste minutes on a doomed run.
    """
    cd_total = probe_duration(countdown)
    cd_visible = float(pip.get("countdown_visible_duration", cd_total))
    if cd_visible > cd_total + 0.05:
        sys.exit(
            f"ERROR: countdown_visible_duration ({cd_visible}s) exceeds "
            f"countdown file duration ({cd_total:.2f}s)."
        )

    errors = []
    for i, f in enumerate(fragments, 1):
        path = Path(f["file"])
        if not path.exists():
            errors.append(f"#{i}: file not found: {path}")
            continue
        try:
            dur = probe_duration(path)
        except Exception as e:
            errors.append(f"#{i}: ffprobe failed on {path}: {e}")
            continue
        end = f["offset"] + f["length"]
        if end > dur + 0.05:
            errors.append(
                f"#{i}: {path} is {dur:.2f}s but fragment needs offset={f['offset']} "
                f"+ length={f['length']} = {end}s"
            )
        cd_len = f.get("countdown_length") or f["length"]
        if cd_len > cd_visible + 0.05:
            errors.append(
                f"#{i}: countdown_length {cd_len}s exceeds countdown_visible_duration {cd_visible}s"
            )
    if errors:
        sys.exit("Config validation failed:\n  - " + "\n  - ".join(errors))


def main():
    ap = argparse.ArgumentParser(
        description="Concatenate videos with crossfades, intro, and PiP countdown."
    )
    ap.add_argument("config", help="YAML config file")
    ap.add_argument("-o", "--output", default="output.mp4")
    ap.add_argument("--workdir", default="./vidmerge_work")
    ap.add_argument("--keep-workdir", action="store_true",
                    help="Keep intermediate normalized clips for debugging")
    ap.add_argument("--test", action="store_true",
                    help="Fast preview: 640x480, 15fps, ultrafast encode. "
                         "PiP dimensions/margin are scaled down proportionally.")
    args = ap.parse_args()

    if args.test:
        apply_test_mode()

    cfg = yaml.safe_load(Path(args.config).read_text())
    workdir = Path(args.workdir)
    workdir.mkdir(exist_ok=True)

    allowed_keys = {"intro", "intervals", "total_minutes", "work_clips", "recover_clips"}
    unknown = set(cfg) - allowed_keys
    if unknown:
        sys.exit(f"ERROR: unknown config keys: {sorted(unknown)}. "
                 f"Allowed: {sorted(allowed_keys)}")

    intro_title = (cfg.get("intro") or {}).get("title", "")
    transition = TRANSITION
    font_path = DEFAULT_FONT
    cd_cfg = {
        "font_size_frac": COUNTDOWN_FONT_SIZE_FRAC,
        "panel_alpha": COUNTDOWN_PANEL_ALPHA,
        "text_color": COUNTDOWN_TEXT_COLOR,
    }
    pip = {"margin": PIP_MARGIN}
    # PiP height = TARGET_H / 8. Width auto-fit to "M:SS" text width plus
    # horizontal padding, so the panel hugs the digits.
    pip_h = max(2, (TARGET_H // 8) & ~1)
    text_w = measure_countdown_text_width(pip_h, cd_cfg)
    pad_px = int(pip_h * PIP_H_PADDING_FRAC)
    pip_w = max(2, (text_w + 2 * pad_px) & ~1)
    pip["width"] = pip_w
    pip["height"] = pip_h

    fragments = build_fragments(cfg)

    # Generate the countdown clip just long enough for the longest visible
    # window we'll need. validate_fragments enforces this against cd_visible.
    longest = max(
        [INTRO_LENGTH] +
        [int(f.get("countdown_length") or f["length"]) for f in fragments]
    )
    countdown_workdir = Path(args.workdir)
    countdown_workdir.mkdir(exist_ok=True)
    countdown = countdown_workdir / "_countdown.mov"
    make_countdown_clip(
        max_seconds=longest,
        size=(pip_w, pip_h),
        cfg=cfg.get("countdown") or {},
        workdir=countdown_workdir,
        out=countdown,
    )
    pip["countdown_visible_duration"] = float(longest)
    validate_fragments(fragments, countdown, pip)

    parts = []

    intro_out = workdir / "00_intro.mp4"
    make_intro(
        intro_title, INTRO_LENGTH,
        font_path, workdir, intro_out,
        countdown, pip,
        transition=transition,
    )
    parts.append(intro_out)

    n = len(fragments)
    for i, frag in enumerate(fragments, 1):
        out = workdir / f"{i:02d}_frag.mp4"
        normalize_fragment(
            frag["file"],
            frag.get("offset", 0),
            frag["length"],
            countdown, pip, out,
            countdown_length=frag.get("countdown_length"),
            countdown_fadeout=frag.get("countdown_fadeout", 1.0),
            countdown_align=frag.get("countdown_align", "end"),
            transition=transition,
            is_first=False,
            is_last=(i == n),
        )
        parts.append(out)

    crossfade_chain(parts, args.output, transition)
    print(f"\n✓ Wrote {args.output}", file=sys.stderr)

    if not args.keep_workdir:
        shutil.rmtree(workdir)


if __name__ == "__main__":
    main()
