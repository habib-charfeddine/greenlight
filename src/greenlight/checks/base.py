"""Shared contract for all checks: context, fetch helpers, finding factory.

Every check module exposes ``run(story, ctx) -> list[Finding]`` at module level.
The engine wraps each module in ``guard()`` so one failing check emits a P3
"check errored" finding instead of killing the run (engine rule #2).

HTTP three-state logic (03_CHECK_CATALOG, T0.asset_reachable):
  OK           2xx/3xx
  UNVERIFIABLE 401/403/407/429/999 -> bot-blocked; P3 info only, never alerts
  BROKEN       404/410/other 4xx, DNS/connection failure, timeout after
               retries, persistent 5xx
Retries: 2 attempts with exponential backoff + jitter, on timeout/5xx only.

Offline mode (the default; tied to --replay): only hosts in settings
``offline_fetch_hosts`` (the local feedgen server) are actually fetched.
External hosts return UNVERIFIABLE without touching the network — the sandbox
can't verify them, the three-state logic says so honestly, and eval runs stay
deterministic. --live lifts the restriction.
"""
from __future__ import annotations

import random
import shutil
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

import httpx

from ..models import Evidence, Finding, Severity, Story
from ..policy import EffectivePolicy

FetchState = Literal["OK", "BROKEN", "UNVERIFIABLE"]

_UNVERIFIABLE_STATUSES = {401, 403, 407, 429, 999}

#: Default severity per check_id (03_CHECK_CATALOG.md). For AI checks this is
#: also the server-side clamp: a judge cannot escalate beyond its check's
#: default severity mapping (judge prompt rule 3, belt and braces).
DEFAULT_SEVERITY: dict[str, Severity] = {
    "T0.schema_valid": "P1",
    "T0.asset_reachable": "P0",
    "T0.asset_type_match": "P1",
    "T0.image_decodes": "P0",
    "T0.image_resolution": "P1",
    "T0.image_blur": "P1",
    "T0.image_solid": "P1",
    "T0.video_probe": "P0",
    "T0.video_black": "P1",
    "T0.video_silent": "P2",
    "T0.cta_link_health": "P0",
    "T0.cta_domain_policy": "P1",   # typosquat escalates to P0 in the check itself
    "T0.placeholder_text": "P1",
    "T0.pii_leak": "P1",
    "T0.profanity_lexicon": "P0",
    "T0.text_style_caps": "P2",
    "T0.text_style_emoji": "P2",
    "T0.date_logic": "P1",
    "T0.dup_asset": "P2",
    "T0.dup_story": "P1",
    "T0.missing_action": "P3",
    "T1.spelling_grammar": "P1",
    "T1.tone_style": "P2",
    "T1.coherence": "P1",
    "T1.cta_copy_mismatch": "P1",
    "T1.safety_screen": "P0",
    "T1.factual_smell": "P2",
    "T1.injection_attempt": "P1",
    "T2.visual_quality": "P1",
    "T2.visual_text": "P1",
    "T2.visual_brand": "P1",
    "T2.visual_safety": "P0",
    "T2.thumb_title_match": "P2",
}


@dataclass
class FetchResult:
    state: FetchState
    status_code: int | None = None
    content_type: str | None = None
    content: bytes | None = None      # populated by fetch_bytes/fetch_file
    path: Path | None = None          # populated by fetch_file
    error: str | None = None


@dataclass
class FFmpegInfo:
    ffmpeg: str | None
    ffprobe: str | None

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)


def detect_ffmpeg() -> FFmpegInfo:
    return FFmpegInfo(ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"))


@dataclass
class CheckContext:
    """Everything a check may need. One instance per ingest cycle."""

    policy: EffectivePolicy
    settings: dict
    http: httpx.Client
    store: Any = None                 # greenlight.store.Store; None in some unit tests
    offline: bool = True              # replay mode: external hosts -> UNVERIFIABLE, no I/O
    today: date = field(default_factory=date.today)
    ffmpeg: FFmpegInfo = field(default_factory=detect_ffmpeg)
    tmpdir: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="greenlight-")))
    story_index: int = 0              # index within the current feed, for json_paths
    _probe_cache: dict[str, FetchResult] = field(default_factory=dict)
    _bytes_cache: dict[str, FetchResult] = field(default_factory=dict)

    # -- HTTP helpers -------------------------------------------------------

    def _timeout(self) -> httpx.Timeout:
        th = self.settings.get("thresholds", {})
        return httpx.Timeout(
            connect=th.get("http_timeout_connect", 3.05),
            read=th.get("http_timeout_read", 10.0),
            write=10.0,
            pool=5.0,
        )

    def _request(self, url: str, read_body: bool) -> FetchResult:
        try:
            host = (httpx.URL(url).host or "").lower()
        except Exception:
            host = ""
        if not host:
            return FetchResult("BROKEN", None, None, error="invalid or relative URL")
        if self.offline:
            allowed = [h.lower() for h in
                       self.settings.get("offline_fetch_hosts", ["localhost", "127.0.0.1"])]
            if host not in allowed:
                return FetchResult("UNVERIFIABLE", None, None,
                                   error="external fetch disabled in offline (replay) mode")
        th = self.settings.get("thresholds", {})
        retries = int(th.get("http_retries", 2))
        max_bytes = int(th.get("fetch_max_bytes", 25 * 1024 * 1024))
        last_error = "unknown"
        for attempt in range(retries + 1):
            try:
                with self.http.stream("GET", url, timeout=self._timeout(),
                                      follow_redirects=True) as r:
                    status = r.status_code
                    ctype = r.headers.get("content-type", "").split(";")[0].strip() or None
                    if 200 <= status < 400:
                        body = b""
                        for chunk in r.iter_bytes(chunk_size=65536):
                            body += chunk
                            if not read_body and len(body) >= 1024:
                                break
                            if len(body) >= max_bytes:
                                break
                        return FetchResult("OK", status, ctype, content=body)
                    if status in _UNVERIFIABLE_STATUSES:
                        return FetchResult("UNVERIFIABLE", status, ctype,
                                           error=f"HTTP {status} (bot-blocked/auth)")
                    if 500 <= status < 600:
                        last_error = f"HTTP {status}"
                        if attempt < retries:
                            time.sleep(0.5 * (2 ** attempt) + random.uniform(0, 0.3))
                            continue
                        return FetchResult("BROKEN", status, ctype, error="persistent 5xx")
                    return FetchResult("BROKEN", status, ctype, error=f"HTTP {status}")
            except httpx.TimeoutException:
                last_error = "timeout"
                if attempt < retries:
                    time.sleep(0.5 * (2 ** attempt) + random.uniform(0, 0.3))
                    continue
                return FetchResult("BROKEN", None, None, error="timeout")
            except httpx.ConnectError as e:
                # DNS failure = a dead domain in the content -> BROKEN.
                # Connection refused/unreachable = infrastructure (server down)
                # -> UNVERIFIABLE, so an asset-server outage never pages a human
                # about "broken content" (05 edge-case list).
                msg = str(e)
                if any(s in msg for s in ("getaddrinfo", "Name or service",
                                          "nodename", "No address")):
                    return FetchResult("BROKEN", None, None, error=f"DNS failure: {msg}")
                return FetchResult("UNVERIFIABLE", None, None,
                                   error=f"connection failed (server down?): {msg}")
            except httpx.HTTPError as e:
                # invalid URL, protocol error
                return FetchResult("BROKEN", None, None, error=f"{type(e).__name__}: {e}")
        return FetchResult("BROKEN", None, None, error=last_error)

    def probe(self, url: str) -> FetchResult:
        """Headers + first KB — link-health checks. Cached per cycle."""
        full = self._bytes_cache.get(url)
        if full is not None:
            return full
        if url not in self._probe_cache:
            self._probe_cache[url] = self._request(url, read_body=False)
        return self._probe_cache[url]

    def fetch_bytes(self, url: str) -> FetchResult:
        """Full body (capped) — media checks. Cached per cycle."""
        if url not in self._bytes_cache:
            self._bytes_cache[url] = self._request(url, read_body=True)
        return self._bytes_cache[url]

    def fetch_file(self, url: str) -> FetchResult:
        """Full body written to a temp file — ffprobe/ffmpeg need a path."""
        result = self.fetch_bytes(url)
        if result.state == "OK" and result.path is None and result.content is not None:
            suffix = Path(httpx.URL(url).path).suffix or ".bin"
            f = tempfile.NamedTemporaryFile(dir=self.tmpdir, suffix=suffix, delete=False)
            f.write(result.content)
            f.close()
            result.path = Path(f.name)
        return result


class Check(Protocol):
    def __call__(self, story: Story, ctx: CheckContext) -> list[Finding]: ...


def make_finding(
    ctx: CheckContext,
    check_id: str,
    summary: str,
    *,
    severity: Severity | None = None,
    evidence: Evidence | None = None,
    suggested_fix: str | None = None,
    tier: int = 0,
    confidence: float = 1.0,
    model: str | None = None,
    prompt_version: str | None = None,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
) -> Finding:
    """Finding factory: stamps policy_version; severity defaults per catalog."""
    return Finding(
        check_id=check_id,
        severity=severity or DEFAULT_SEVERITY.get(check_id, "P3"),
        confidence=confidence,
        summary=summary,
        evidence=evidence or Evidence(),
        suggested_fix=suggested_fix,
        tier=tier,
        model=model,
        policy_version=ctx.policy.policy_version,
        prompt_version=prompt_version,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )


def guard(name: str, fn: Callable[[Story, CheckContext], list[Finding]],
          story: Story, ctx: CheckContext) -> list[Finding]:
    """Engine rule #2: a crashing check emits a P3 finding, never kills the run."""
    try:
        return fn(story, ctx)
    except Exception as e:  # noqa: BLE001 — deliberate blanket guard
        traceback.print_exc()
        return [
            make_finding(
                ctx,
                f"T0.{name}" if not name.startswith(("T0.", "T1.", "T2.")) else name,
                f"check errored: {type(e).__name__}: {e}",
                severity="P3",
                tier=0,
            )
        ]
