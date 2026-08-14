"""Tier 0 media quality checks (03_CHECK_CATALOG.md, T0.image_* / T0.video_*).

Image checks decode from bytes fetched via ``ctx.fetch_bytes``; video checks
run ffprobe/ffmpeg against a temp file from ``ctx.fetch_file``. Pages whose
asset fetch is BROKEN or UNVERIFIABLE are skipped entirely here — link health
is owned by the links module and double-reporting would be noise.

If ffmpeg/ffprobe are not installed, each video page gets exactly one P3
``T0.video_probe`` finding whose summary starts with "skipped:" — the engine
counts those as skipped checks, and the run never crashes on a missing binary.
"""
from __future__ import annotations

import json
import re
import subprocess
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ..models import Evidence, Finding, Page, Story
from .base import CheckContext, make_finding

#: Wall-clock cap per ffprobe/ffmpeg invocation. This is an ops guard against a
#: hung subprocess, not a content-quality threshold, so it is not in settings.yaml.
_SUBPROCESS_TIMEOUT_S = 30

_BLACK_SEGMENT_RE = re.compile(r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)")
_MAX_VOLUME_RE = re.compile(r"max_volume:\s*(-?[0-9.]+)\s*dB")


def run(story: Story, ctx: CheckContext) -> list[Finding]:
    """Run all media checks for one story. Returns findings, never raises."""
    findings: list[Finding] = []
    for index, page in enumerate(story.pages):
        if page.type == "image":
            findings.extend(_check_image(page, index, ctx))
        elif page.type == "video":
            findings.extend(_check_video(page, index, ctx))
    return findings


# -- images -----------------------------------------------------------------


def _check_image(page: Page, index: int, ctx: CheckContext) -> list[Finding]:
    """Decode once, then measure. Undecodable image short-circuits (P0)."""
    result = ctx.fetch_bytes(page.asset_url)
    if result.state != "OK" or not result.content:
        return []  # BROKEN/UNVERIFIABLE is the links module's finding, not ours
    json_path = f"pages[{index}].asset_url"
    if result.truncated:
        # A capped download would decode as "corrupt" — that's our cap, not
        # the tenant's asset. Say so instead of misdiagnosing.
        return [make_finding(
            ctx, "T0.image_decodes",
            "skipped: asset exceeds the fetch-size cap — media quality not assessed",
            severity="P3",
            evidence=Evidence(json_path=json_path, url=page.asset_url,
                              measured=f"fetched={len(result.content)}B (capped)"),
        )]
    img, error = _decode_pil(result.content)
    if img is None:
        return [
            make_finding(
                ctx,
                "T0.image_decodes",
                f"image fails to decode: {error}",
                evidence=Evidence(json_path=json_path, url=page.asset_url),
                suggested_fix="Re-export the image; the uploaded file is corrupt or truncated.",
            )
        ]
    findings = _image_resolution(img, page, json_path, ctx)
    gray = _decode_gray(result.content)
    if gray is not None:
        # cv2 can occasionally reject a format PIL accepts; blur/solid are then
        # unmeasurable and we stay quiet — decodability was already proven above.
        findings.extend(_image_blur(gray, page, json_path, ctx))
        findings.extend(_image_solid(gray, page, json_path, ctx))
    return findings


def _decode_pil(data: bytes) -> tuple[Image.Image | None, str | None]:
    """T0.image_decodes: PIL open+verify, then re-open for use.

    ``verify()`` invalidates the image object, so a fresh ``open`` + ``load``
    follows; ``load`` forces a full decode and catches truncated payloads that
    ``verify`` alone can miss. WHY P0: an undecodable asset renders as a broken
    tile in the story player — the page is unusable, not merely ugly.
    """
    try:
        probe = Image.open(BytesIO(data))
        probe.verify()
        img = Image.open(BytesIO(data))
        img.load()
        return img, None
    except Exception as e:  # noqa: BLE001 — any decode failure is the finding
        return None, f"{type(e).__name__}: {e}"


def _decode_gray(data: bytes) -> np.ndarray | None:
    """Grayscale ndarray for blur/solid metrics, or None if cv2 cannot decode."""
    bgr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        return None
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def _image_resolution(
    img: Image.Image, page: Page, json_path: str, ctx: CheckContext
) -> list[Finding]:
    """T0.image_resolution: minimum size, then vertical-aspect conformance.

    Below ``image_min_width`` x ``image_min_height`` (640x1136 default) -> P1:
    a full-screen 9:16 story visibly upscales anything smaller. Otherwise, if
    width/height deviates from the ideal frame ratio (``image_ideal_width`` /
    ``image_ideal_height``, 1080x1920 = 9:16) by more than ``aspect_tolerance``
    (0.10 relative) -> P2: the player letterboxes or crops landscape/near-square
    art, which is a polish problem, not breakage — hence the lower severity.
    """
    min_w = int(ctx.policy.threshold("image_min_width"))
    min_h = int(ctx.policy.threshold("image_min_height"))
    width, height = img.size
    if width < min_w or height < min_h:
        return [
            make_finding(
                ctx,
                "T0.image_resolution",
                f"image below minimum resolution at {width}x{height}",
                evidence=Evidence(
                    json_path=json_path,
                    url=page.asset_url,
                    measured=f"{width}x{height}",
                    threshold=f"min={min_w}x{min_h}",
                ),
                suggested_fix=f"Export at the ideal story size or at least {min_w}x{min_h}.",
            )
        ]
    ideal_w = int(ctx.policy.threshold("image_ideal_width"))
    ideal_h = int(ctx.policy.threshold("image_ideal_height"))
    tolerance = float(ctx.policy.threshold("aspect_tolerance"))
    ideal = ideal_w / ideal_h
    actual = width / height
    if abs(actual - ideal) / ideal > tolerance:
        return [
            make_finding(
                ctx,
                "T0.image_resolution",
                f"image aspect {width}x{height} is not vertical 9:16 (landscape/letterbox)",
                severity="P2",
                evidence=Evidence(
                    json_path=json_path,
                    url=page.asset_url,
                    measured=f"{width}x{height} aspect={actual:.3f}",
                    threshold=f"ideal={ideal_w}x{ideal_h} tolerance={tolerance:.0%}",
                ),
                suggested_fix=f"Reframe the asset to {ideal_w}x{ideal_h} (9:16 vertical).",
            )
        ]
    return []


def _image_blur(
    gray: np.ndarray, page: Page, json_path: str, ctx: CheckContext
) -> list[Finding]:
    """T0.image_blur: variance of the Laplacian below ``blur_laplacian_min``.

    Default threshold 100 -> P1. WHY: low Laplacian variance means few sharp
    edges — the classic cheap blur detector. It is content-dependent (flat,
    minimalist graphics legitimately score low), which is exactly why the
    threshold is tenant-tunable via the tenant ``media.blur_laplacian_min``
    override rather than fixed in code.
    """
    blur_min = float(ctx.policy.threshold("blur_laplacian_min"))
    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if variance >= blur_min:
        return []
    return [
        make_finding(
            ctx,
            "T0.image_blur",
            f"image appears blurry (Laplacian variance {variance:.1f})",
            evidence=Evidence(
                json_path=json_path,
                url=page.asset_url,
                measured=f"laplacian_var={variance:.1f}",
                threshold=f"min={blur_min:g}",
            ),
            suggested_fix="Replace with a sharp source asset; avoid upscaled or re-compressed art.",
        )
    ]


def _image_solid(
    gray: np.ndarray, page: Page, json_path: str, ctx: CheckContext
) -> list[Finding]:
    """T0.image_solid: near-uniform or near-black frame.

    Grayscale stddev below ``solid_stddev_max`` (5) OR mean below
    ``solid_mean_max`` (10) -> P1. WHY: a frame with essentially no pixel
    variation (solid colour) or almost no luminance (black) is nearly always a
    failed render/export, and it occupies a full story page.
    """
    stddev_max = float(ctx.policy.threshold("solid_stddev_max"))
    mean_max = float(ctx.policy.threshold("solid_mean_max"))
    stddev = float(gray.std())
    mean = float(gray.mean())
    if stddev >= stddev_max and mean >= mean_max:
        return []
    return [
        make_finding(
            ctx,
            "T0.image_solid",
            "image is a solid or near-black frame",
            evidence=Evidence(
                json_path=json_path,
                url=page.asset_url,
                measured=f"stddev={stddev:.1f} mean={mean:.1f}",
                threshold=f"min_stddev={stddev_max:g} or min_mean={mean_max:g}",
            ),
            suggested_fix="Check the export pipeline; the frame rendered empty.",
        )
    ]


# -- videos -----------------------------------------------------------------


def _check_video(page: Page, index: int, ctx: CheckContext) -> list[Finding]:
    """Probe first; deeper scans only run on a file ffprobe says is playable."""
    json_path = f"pages[{index}].asset_url"
    if not ctx.ffmpeg.available:
        return [
            make_finding(
                ctx,
                "T0.video_probe",
                "skipped: ffmpeg not installed; video checks did not run",
                severity="P3",
                evidence=Evidence(json_path=json_path, url=page.asset_url),
                suggested_fix="Install ffmpeg/ffprobe on the checker host to enable video checks.",
            )
        ]
    result = ctx.fetch_file(page.asset_url)
    if result.state != "OK" or result.path is None:
        return []  # BROKEN/UNVERIFIABLE is the links module's finding, not ours
    if result.truncated:
        return [make_finding(
            ctx, "T0.video_probe",
            "skipped: asset exceeds the fetch-size cap — video checks did not run",
            severity="P3",
            evidence=Evidence(json_path=json_path, url=page.asset_url,
                              measured=f"fetched={len(result.content or b'')}B (capped)"),
        )]
    findings, duration, has_audio, playable = _video_probe(result.path, page, json_path, ctx)
    if playable:
        findings.extend(_video_black(result.path, duration, page, json_path, ctx))
        if has_audio:
            findings.extend(_video_silent(result.path, page, json_path, ctx))
    return findings


def _run_subprocess(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run ffprobe/ffmpeg with a hard timeout; raises TimeoutExpired/OSError."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )


def _subprocess_errored(
    ctx: CheckContext, check_id: str, detail: str, page: Page, json_path: str
) -> Finding:
    """P3 'check errored' finding: a tooling failure must never look like a
    content failure, and must never kill the run (engine rule #2, kept local
    so one bad video cannot suppress the other checks on the same story)."""
    return make_finding(
        ctx,
        check_id,
        f"check errored: {detail}",
        severity="P3",
        evidence=Evidence(json_path=json_path, url=page.asset_url),
    )


def _video_probe(
    video_path: Path, page: Page, json_path: str, ctx: CheckContext
) -> tuple[list[Finding], float | None, bool, bool]:
    """T0.video_probe: container sanity via ffprobe JSON.

    Rules: ffprobe failure, unparseable output, or no video stream -> P0
    unplayable (the player cannot show the page at all). Duration below
    ``video_min_seconds`` (1) -> P0: sub-second files are truncated-upload
    stubs. Duration above ``video_max_seconds`` (120, tenant-tunable) -> P1:
    overlong is a policy breach, not breakage. No audio stream -> P2: some
    tenants ship intentionally silent graphic loops, so the default stays low;
    a tenant that requires audio bumps it via ``severity_overrides`` (applied
    by the engine, not here).

    Returns (findings, duration_seconds, has_audio, playable).
    """
    cmd = [
        ctx.ffmpeg.ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(video_path),
    ]
    try:
        proc = _run_subprocess(cmd)
    except (subprocess.TimeoutExpired, OSError) as e:
        detail = f"ffprobe {type(e).__name__}"
        return [_subprocess_errored(ctx, "T0.video_probe", detail, page, json_path)], None, False, False

    def unplayable(reason: str, measured: str) -> Finding:
        return make_finding(
            ctx,
            "T0.video_probe",
            f"unplayable video: {reason}",
            evidence=Evidence(
                json_path=json_path, url=page.asset_url, measured=measured
            ),
            suggested_fix="Re-encode and re-upload the video; the file is not a playable container.",
        )

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()
        detail = stderr[-1][:160] if stderr else f"exit code {proc.returncode}"
        return [unplayable("ffprobe could not read the file", detail)], None, False, False
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return [unplayable("ffprobe output was not parseable", "invalid ffprobe JSON")], None, False, False

    streams = data.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not has_video:
        codecs = ",".join(s.get("codec_type", "?") for s in streams) or "none"
        return [unplayable("no video stream", f"streams={codecs}")], None, has_audio, False

    duration = _parse_duration(data, streams)
    findings: list[Finding] = []
    min_s = float(ctx.policy.threshold("video_min_seconds"))
    max_s = float(ctx.policy.threshold("video_max_seconds"))
    if duration is not None and duration < min_s:
        findings.append(
            make_finding(
                ctx,
                "T0.video_probe",
                f"video too short: {duration:.1f}s stub",
                evidence=Evidence(
                    json_path=json_path,
                    url=page.asset_url,
                    measured=f"duration={duration:.1f}s",
                    threshold=f"min={min_s:g}s",
                ),
                suggested_fix="Re-upload the full clip; this file is a truncated stub.",
            )
        )
    elif duration is not None and duration > max_s:
        findings.append(
            make_finding(
                ctx,
                "T0.video_probe",
                f"video too long: {duration:.1f}s",
                severity="P1",
                evidence=Evidence(
                    json_path=json_path,
                    url=page.asset_url,
                    measured=f"duration={duration:.1f}s",
                    threshold=f"max={max_s:g}s",
                ),
                suggested_fix=f"Trim the clip to at most {max_s:g}s for story playback.",
            )
        )
    if not has_audio:
        findings.append(
            make_finding(
                ctx,
                "T0.video_probe",
                "video has no audio stream",
                severity="P2",
                evidence=Evidence(
                    json_path=json_path, url=page.asset_url, measured="audio_streams=0"
                ),
            )
        )
    return findings, duration, has_audio, True


def _parse_duration(data: dict, streams: list[dict]) -> float | None:
    """Duration from format, falling back to the video stream; None if absent."""
    raw = (data.get("format") or {}).get("duration")
    if raw is None:
        for s in streams:
            if s.get("codec_type") == "video" and s.get("duration"):
                raw = s["duration"]
                break
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _video_black(
    video_path: Path,
    duration: float | None,
    page: Page,
    json_path: str,
    ctx: CheckContext,
) -> list[Finding]:
    """T0.video_black: blackdetect scan for dead footage.

    Flags P1 when total black time exceeds ``video_black_ratio`` (0.30) of the
    duration, OR a black segment starting within 0.1s runs at least
    ``video_black_open_s`` (2.0s). WHY: mostly-black clips are failed renders,
    and a long black open reads as a broken page to a viewer who swipes past
    before content appears. Detector params ``d=1.0:pix_th=0.10`` are fixed by
    the check catalog (segment granularity, not a quality threshold).
    """
    cmd = [
        ctx.ffmpeg.ffmpeg,
        "-i", str(video_path),
        "-vf", "blackdetect=d=1.0:pix_th=0.10",
        "-an", "-f", "null", "-",
    ]
    try:
        proc = _run_subprocess(cmd)
    except (subprocess.TimeoutExpired, OSError) as e:
        return [_subprocess_errored(ctx, "T0.video_black", f"ffmpeg {type(e).__name__}", page, json_path)]
    if proc.returncode != 0:
        return [_subprocess_errored(ctx, "T0.video_black", f"ffmpeg exit code {proc.returncode}", page, json_path)]

    segments = [(float(s), float(e)) for s, e in _BLACK_SEGMENT_RE.findall(proc.stderr or "")]
    if not segments:
        return []
    ratio_max = float(ctx.policy.threshold("video_black_ratio"))
    open_min_s = float(ctx.policy.threshold("video_black_open_s"))
    total_black = sum(end - start for start, end in segments)

    if duration is not None and duration > 0 and total_black / duration > ratio_max:
        return [
            make_finding(
                ctx,
                "T0.video_black",
                f"video is {total_black / duration:.0%} black frames",
                evidence=Evidence(
                    json_path=json_path,
                    url=page.asset_url,
                    frame_ts=segments[0][0],
                    measured=f"black_total={total_black:.1f}s of {duration:.1f}s",
                    threshold=f"max_ratio={ratio_max:g}",
                ),
                suggested_fix="Re-render the clip; most of its runtime is black.",
            )
        ]
    for start, end in segments:
        if start <= 0.1 and (end - start) >= open_min_s:
            return [
                make_finding(
                    ctx,
                    "T0.video_black",
                    f"video opens with {end - start:.1f}s of black",
                    evidence=Evidence(
                        json_path=json_path,
                        url=page.asset_url,
                        frame_ts=start,
                        measured=f"black_open={end - start:.1f}s",
                        threshold=f"max_open={open_min_s:g}s",
                    ),
                    suggested_fix="Trim the black lead-in so the page shows content immediately.",
                )
            ]
    return []


def _video_silent(
    video_path: Path, page: Page, json_path: str, ctx: CheckContext
) -> list[Finding]:
    """T0.video_silent: volumedetect on files that DO have an audio stream.

    max_volume below ``silent_max_volume_db`` (-50 dB) -> P2. WHY: an audio
    track that peaks under -50 dB is inaudible in practice — a muxed-but-empty
    track, which usually means the audio export failed. Only runs when the
    probe found an audio stream; a missing stream is T0.video_probe's P2.
    """
    cmd = [
        ctx.ffmpeg.ffmpeg,
        "-i", str(video_path),
        "-af", "volumedetect",
        "-f", "null", "-",
    ]
    try:
        proc = _run_subprocess(cmd)
    except (subprocess.TimeoutExpired, OSError) as e:
        return [_subprocess_errored(ctx, "T0.video_silent", f"ffmpeg {type(e).__name__}", page, json_path)]
    if proc.returncode != 0:
        return [_subprocess_errored(ctx, "T0.video_silent", f"ffmpeg exit code {proc.returncode}", page, json_path)]

    match = _MAX_VOLUME_RE.search(proc.stderr or "")
    if match is None:
        return []  # volumedetect produced no reading; nothing measurable to flag
    max_volume = float(match.group(1))
    silent_db = float(ctx.policy.threshold("silent_max_volume_db"))
    if max_volume >= silent_db:
        return []
    return [
        make_finding(
            ctx,
            "T0.video_silent",
            f"video audio is effectively silent (max_volume {max_volume:.1f} dB)",
            evidence=Evidence(
                json_path=json_path,
                url=page.asset_url,
                measured=f"max_volume={max_volume:.1f}dB",
                threshold=f"min={silent_db:g}dB",
            ),
            suggested_fix="Check the audio export; the track is present but carries no signal.",
        )
    ]
