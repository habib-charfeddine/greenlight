"""Deterministic local asset synthesis for the mock feed.

Images are drawn with PIL (club-colored vertical gradient, large burned-in
title, footer bar); videos are rendered with the ffmpeg CLI (gradients source
plus a 440 Hz sine audio track — no drawtext, so no font-path pain on Windows).

Every asset is cached in ``feedgen/.cache`` keyed by a sha1 of its full
parameter set, then copied into the world's assets directory: repeat runs with
the same seed are instant and byte-identical.

Variant catalog (05_DATA_AND_EVAL.md "Asset synthesis"):
  images: clean, blurry (GaussianBlur radius 8), tiny (320x568),
          landscape (1920x1080), near_black, burned_typo (renders exactly
          "SEALS UNTIED -- TICKETS TDB" with an em-dash), near_duplicate
          (same params as its base + brightness +6/255), truncated (clean
          jpg cut to the first 40% of bytes)
  videos: clean (8s 540x960 + sine), black_open (3s black then content),
          silent (digital-silence audio track — volumedetect territory),
          stub (0.4s), corrupt (clean file cut to the first 30% of bytes)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

_CACHE_VERSION = "2"   # bump whenever a variant's render command changes

IMAGE_SIZE = (1080, 1920)
TINY_SIZE = (320, 568)
LANDSCAPE_SIZE = (1920, 1080)
BLUR_RADIUS = 8
BRIGHTNESS_STEP = 6           # near_duplicate: +6/255 per channel
TRUNCATE_IMAGE_RATIO = 0.4    # keep first 40% of jpg bytes
TRUNCATE_VIDEO_RATIO = 0.3    # keep first 30% of mp4 bytes

VIDEO_SIZE = "540x960"
VIDEO_SECONDS = 8.0
BLACK_OPEN_SECONDS = 3.0
STUB_SECONDS = 0.4
SINE_HZ = 440

#: Exact burned-in typo string for seeded defect row 26 (T2.visual_text).
BURNED_TYPO_TEXT = "SEALS UNTIED \u2014 TICKETS TDB"

IMAGE_VARIANTS = {"clean", "blurry", "tiny", "landscape", "near_black",
                  "burned_typo", "near_duplicate", "truncated"}
VIDEO_VARIANTS = {"clean", "black_open", "silent", "stub", "corrupt"}

#: club -> (gradient top, gradient bottom, accent) hex colors.
CLUB_STYLES: dict[str, tuple[str, str, str]] = {
    "Penguin FC": ("#10141c", "#f07f24", "#f8c471"),
    "Seals United": ("#123a6d", "#9fc3e8", "#4f86c6"),
    "Iceberg Rovers": ("#0d5c66", "#bfeef2", "#4fb3bf"),
    "Frostbite Wanderers": ("#2c2560", "#c9c2f0", "#7d6fd1"),
}
_DEFAULT_STYLE = ("#30343c", "#8a909c", "#c8ccd4")

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "DejaVuSans-Bold.ttf",
)


# -- shared helpers ---------------------------------------------------------

def _cache_key(params: dict) -> str:
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _style(club: str) -> tuple[str, str, str]:
    return CLUB_STYLES.get(club, _DEFAULT_STYLE)


def _font(size: int) -> ImageFont.ImageFont:
    """Best available bold font: Arial Bold, Arial, DejaVu, PIL builtin."""
    for candidate in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _printable(text: str) -> str:
    """Strip glyphs our fonts cannot render (emoji); keep dashes/ellipsis."""
    kept = "".join(c for c in text if ord(c) < 0x2500)
    return " ".join(kept.split())


def _copy_out(cached: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached, dest)
    return dest


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# -- images -----------------------------------------------------------------

def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = (current + " " + word).strip()
        if not current or draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _motif_overlay(img: Image.Image, accent: str, motif: int) -> Image.Image:
    """Large translucent circles seeded by ``motif``.

    Gives every clean image a distinct structure so pHash does not treat all
    gradient covers as near-duplicates of one another (dup_asset FP guard).
    """
    rng = random.Random(motif)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    r, g, b = _rgb(accent)
    for _ in range(rng.randint(3, 6)):
        radius = rng.randint(img.size[0] // 6, img.size[0] // 2)
        cx = rng.randint(0, img.size[0])
        cy = rng.randint(0, img.size[1])
        alpha = rng.randint(40, 90)
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                     fill=(r, g, b, alpha))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _compose(size: tuple[int, int], title: str, club: str, motif: int) -> Image.Image:
    top, bottom, accent = _style(club)
    gradient = Image.linear_gradient("L").resize(size)
    img = ImageOps.colorize(gradient, black=top, white=bottom).convert("RGB")
    img = _motif_overlay(img, accent, motif)
    draw = ImageDraw.Draw(img)

    text = _printable(title)
    if text:
        font_size = max(48, size[1] // 20)
        font = _font(font_size)
        lines = _wrap(draw, text, font, size[0] - 160)
        y = int(size[1] * 0.16)
        for line in lines[:5]:
            draw.text((84, y + 4), line, font=font, fill=(0, 0, 0))
            draw.text((80, y), line, font=font, fill=(255, 255, 255))
            y += int(font_size * 1.2)

    bar_h = max(90, size[1] // 14)
    draw.rectangle([0, size[1] - bar_h, size[0], size[1]], fill=(14, 16, 22))
    footer_font = _font(max(24, bar_h // 3))
    draw.text((40, size[1] - bar_h + bar_h // 3), club.upper(),
              font=footer_font, fill=(230, 230, 235))
    return img


def _near_black_image() -> Image.Image:
    """Almost-solid dark frame: mean ~6, stddev ~1.5 — well inside the
    ``image_solid`` thresholds (stddev < 5 or mean < 10). No text/motif,
    which would raise the variance."""
    w, h = IMAGE_SIZE
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        v = 3 + (5 * y) // h
        draw.line([(0, y), (w, y)], fill=(v, v, v + 1))
    return img


def _render_image_bytes(variant: str, title: str, club: str, motif: int) -> bytes:
    if variant == "near_black":
        img = _near_black_image()
    else:
        size = LANDSCAPE_SIZE if variant == "landscape" else IMAGE_SIZE
        text = BURNED_TYPO_TEXT if variant == "burned_typo" else title
        img = _compose(size, text, club, motif)
        if variant == "blurry":
            img = img.filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
        elif variant == "tiny":
            img = img.resize(TINY_SIZE, Image.LANCZOS)
        elif variant == "near_duplicate":
            img = img.point(lambda v: min(255, v + BRIGHTNESS_STEP))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    data = buf.getvalue()
    if variant == "truncated":
        data = data[: int(len(data) * TRUNCATE_IMAGE_RATIO)]
    return data


def ensure_image(dest: Path, cache_dir: Path, *, variant: str, title: str,
                 club: str, motif: int, seed: int) -> Path:
    """Synthesize (or reuse cached) image asset and copy it to ``dest``."""
    if variant not in IMAGE_VARIANTS:
        raise ValueError(f"unknown image variant: {variant!r}")
    key = _cache_key({"kind": "image", "variant": variant, "title": title,
                      "club": club, "motif": motif, "seed": seed,
                      "v": _CACHE_VERSION})
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{key}.jpg"
    if not cached.exists():
        _write_atomic(cached, _render_image_bytes(variant, title, club, motif))
    return _copy_out(cached, dest)


# -- videos -----------------------------------------------------------------

def _ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError(
            "ffmpeg not found on PATH -- feedgen needs the ffmpeg CLI to "
            "synthesize video assets (winget install Gyan.FFmpeg)"
        )
    return path


def _run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-500:]
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {tail}")


def _gradients_src(duration: float, club: str, vseed: int) -> str:
    top, bottom, _ = _style(club)
    return (f"gradients=s={VIDEO_SIZE}:r=25:d={duration}:speed=0.02"
            f":seed={vseed % 2147483647}"
            f":c0=0x{top.lstrip('#')}:c1=0x{bottom.lstrip('#')}")


def _sine_src(duration: float) -> str:
    return f"sine=frequency={SINE_HZ}:duration={duration}"


_ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-threads", "1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
           "-flags:a", "+bitexact", "-map_metadata", "-1"]
_AUDIO = ["-c:a", "aac", "-b:a", "96k"]


def _render_video(out_path: Path, variant: str, club: str, vseed: int) -> None:
    ffmpeg = _ffmpeg_path()
    tmp = out_path.with_name(out_path.name + ".part.mp4")
    base = [ffmpeg, "-y", "-loglevel", "error"]

    if variant == "clean":
        cmd = base + ["-f", "lavfi", "-i", _gradients_src(VIDEO_SECONDS, club, vseed),
                      "-f", "lavfi", "-i", _sine_src(VIDEO_SECONDS),
                      "-map", "0:v", "-map", "1:a",
                      *_ENCODE, *_AUDIO, "-shortest", str(tmp)]
    elif variant == "silent":
        # A digital-silence AUDIO TRACK, not -an: T0.video_silent measures
        # volumedetect on an existing stream (catalog wording); a missing
        # stream is video_probe's no-audio P2 instead — different defect.
        cmd = base + ["-f", "lavfi", "-i", _gradients_src(VIDEO_SECONDS, club, vseed),
                      "-f", "lavfi", "-i",
                      f"anullsrc=r=44100:cl=mono:d={VIDEO_SECONDS}",
                      "-map", "0:v", "-map", "1:a",
                      *_ENCODE, *_AUDIO, "-shortest", str(tmp)]
    elif variant == "stub":
        cmd = base + ["-f", "lavfi", "-i", _gradients_src(STUB_SECONDS, club, vseed),
                      "-f", "lavfi", "-i", _sine_src(STUB_SECONDS),
                      "-map", "0:v", "-map", "1:a",
                      *_ENCODE, *_AUDIO, "-shortest", str(tmp)]
    elif variant == "black_open":
        rest = VIDEO_SECONDS - BLACK_OPEN_SECONDS
        cmd = base + [
            "-f", "lavfi", "-i", f"color=c=black:s={VIDEO_SIZE}:r=25:d={BLACK_OPEN_SECONDS}",
            "-f", "lavfi", "-i", _gradients_src(rest, club, vseed),
            "-f", "lavfi", "-i", _sine_src(VIDEO_SECONDS),
            "-filter_complex",
            "[0:v]format=yuv420p[b0];[1:v]format=yuv420p[b1];"
            "[b0][b1]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-map", "2:a",
            *_ENCODE, *_AUDIO, "-shortest", str(tmp)]
    else:
        raise ValueError(f"unknown video variant: {variant!r}")

    _run_ffmpeg(cmd)
    os.replace(tmp, out_path)


def ensure_video(dest: Path, cache_dir: Path, *, variant: str, club: str,
                 vseed: int, seed: int) -> Path:
    """Synthesize (or reuse cached) video asset and copy it to ``dest``."""
    if variant not in VIDEO_VARIANTS:
        raise ValueError(f"unknown video variant: {variant!r}")
    key = _cache_key({"kind": "video", "variant": variant, "club": club,
                      "vseed": vseed, "seed": seed, "v": _CACHE_VERSION})
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{key}.mp4"
    if not cached.exists():
        if variant == "corrupt":
            whole = cache_dir / f"{key}.whole.mp4"
            _render_video(whole, "clean", club, vseed)
            data = whole.read_bytes()
            whole.unlink()
            _write_atomic(cached, data[: int(len(data) * TRUNCATE_VIDEO_RATIO)])
        else:
            _render_video(cached, variant, club, vseed)
    return _copy_out(cached, dest)
