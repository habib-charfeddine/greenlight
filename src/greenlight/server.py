"""FastAPI triage dashboard (milestone M4, 06_DASHBOARD_SPEC).

Server-rendered Jinja2 pages, one CSS file, ~60 lines of vanilla JS.
The spec suggests htmx from a CDN for polling; the demo must run fully
offline, so ``static/app.js`` polls ``GET /partials/queue`` with fetch()
every 8s instead — same behaviour, zero external dependencies.

This module only exposes ``create_app(store, settings, tenants)``; the CLI
owns uvicorn (no ``__main__`` block here by design).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .models import SEVERITY_ORDER, utcnow
from .policy import TenantPolicy
from .store import Store

#: Queue band order: RED needs eyes first, GREEN last (06 spec).
_BAND_RANK = {"RED": 0, "AMBER": 1, "GREEN": 2}

#: One-level severity downgrade for low-confidence findings (engine rule #5).
_DOWNGRADE = {"P0": "P1", "P1": "P2", "P2": "P3", "P3": "P3"}

#: Dismiss-rate at which /metrics tags a check "candidate to auto-quiet".
#: A reviewer rejecting half of a check's findings means the check is noise;
#: UI-only heuristic (not a settings.yaml knob — nothing enforces it yet).
_AUTO_QUIET_DISMISS_RATE = 0.5

#: Confidence bar track width in px (06 spec: 60px hairline track, ink fill).
_CONF_BAR_PX = 60


# -- Jinja filters ----------------------------------------------------------

def _parse_iso(value: str | None) -> datetime | None:
    """ISO timestamp string -> aware UTC datetime, or None if unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _reltime(value: str | None) -> str:
    """'3m ago' style relative time; the absolute goes in the title attr."""
    dt = _parse_iso(value)
    if dt is None:
        return "never"
    seconds = max(0, int((utcnow() - dt).total_seconds()))
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _money(value: Any) -> str:
    """USD with 4 decimals ($0.0042) — per-story AI costs live in the mills."""
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "$0.0000"


# -- view-model helpers (pure, unit-testable) --------------------------------

def _finding_sort_key(f: dict) -> tuple[int, float]:
    """Most severe first, then most confident — used for top-finding picks."""
    return (SEVERITY_ORDER.get(f.get("severity", "P3"), 4),
            -float(f.get("confidence", 0.0)))


def _severity_counts(findings: list[dict]) -> list[tuple[str, int]]:
    """Non-zero (severity, count) pairs in P0..P3 order for the row badges."""
    counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for f in findings:
        sev = f.get("severity")
        if sev in counts:
            counts[sev] += 1
    return [(sev, n) for sev, n in counts.items() if n]


def _first_thumb(story: dict) -> str:
    """First image page's asset_url; '' means fall back to the letter block
    (video keyframes need ffmpeg — the letter block is the honest fallback)."""
    for page in story.get("pages") or []:
        if isinstance(page, dict) and page.get("type") == "image" and page.get("asset_url"):
            return str(page["asset_url"])
    return ""


def _queue_view(rows: list[dict], tenant_names: dict[str, str]) -> list[dict]:
    """Triage order: RED first, then AMBER by score asc (worst AMBER on top),
    then GREEN; newest first within a band. ``rows`` arrive newest-first from
    the store and Python's sort is stable, so one keyed sort suffices."""
    ordered = sorted(rows, key=lambda r: (
        _BAND_RANK.get(r["verdict"], 3),
        r["score"] if r["verdict"] == "AMBER" else 0,
    ))
    view = []
    for r in ordered:
        story = r.get("story") or {}
        findings = r.get("findings") or []
        top = min(findings, key=_finding_sort_key) if findings else None
        title = story.get("story_title") or r["story_id"]
        view.append({
            "story_id": r["story_id"],
            "story_title": title,
            "tenant_name": tenant_names.get(r["tenant_id"], r["tenant_id"]),
            "verdict": r["verdict"],
            "score": r["score"],
            "checked_at": r["checked_at"],
            "cost_usd": r["cost_usd"],
            "top_summary": top["summary"] if top else "",
            "sev_counts": _severity_counts(findings),
            "thumb_url": _first_thumb(story),
            "letter": (str(title).strip()[:1] or "?").upper(),
        })
    return view


def _effective_review(findings: list[dict], feedback_rows: list[dict],
                      settings: dict) -> tuple[str, int, dict[str, str]]:
    """Verdict as it stands after review, recomputed with engine rule #5:

    drop dismissed findings (latest reviewer action per check_id wins), then
    downgrade one severity level when confidence is below the settings
    ``confidence_downgrade_below`` floor (deterministic checks are always
    1.0, so only AI findings can trip this), then RED on any P0, AMBER on
    any P1, else GREEN; score = max(0, 100 - sum of ``severity_weights``).
    """
    latest: dict[str, str] = {}
    for row in feedback_rows:          # rows are oldest-first; last one wins
        latest[row["check_id"]] = row["action"]
    weights = settings.get("severity_weights") or {}
    floor = float(settings.get("confidence_downgrade_below", 0.70))
    effective: list[str] = []
    for f in findings:
        if latest.get(f["check_id"]) == "dismiss":
            continue
        sev = f.get("severity", "P3")
        if float(f.get("confidence", 1.0)) < floor:
            sev = _DOWNGRADE.get(sev, sev)
        effective.append(sev)
    score = max(0, 100 - sum(int(weights.get(sev, 0)) for sev in effective))
    if "P0" in effective:
        verdict = "RED"
    elif "P1" in effective:
        verdict = "AMBER"
    else:
        verdict = "GREEN"
    return verdict, score, latest


def _headline(findings: list[dict]) -> str | None:
    """Judge-written headline if the engine stored one (check_id
    ``T1._headline``); otherwise the top finding's summary; None when clean."""
    for f in findings:
        if f.get("check_id") == "T1._headline":
            return f.get("summary")
    if findings:
        return min(findings, key=_finding_sort_key).get("summary")
    return None


def _evidence_lines(ev: dict) -> list[str]:
    """Monospace evidence block: one aligned line per non-null Evidence field."""
    lines: list[str] = []
    if ev.get("json_path"):
        lines.append(f"json_path  {ev['json_path']}")
    if ev.get("url"):
        status = ev.get("http_status")
        suffix = f"  -> HTTP {status}" if status is not None else ""
        lines.append(f"url        {ev['url']}{suffix}")
    elif ev.get("http_status") is not None:
        lines.append(f"http       HTTP {ev['http_status']}")
    if ev.get("frame_ts") is not None:
        lines.append(f"frame_ts   {ev['frame_ts']}s")
    if ev.get("excerpt"):
        lines.append(f'excerpt    "{ev["excerpt"]}"')
    if ev.get("measured") is not None or ev.get("threshold") is not None:
        measured = ev.get("measured") or "-"
        threshold = ev.get("threshold")
        suffix = f"  (threshold {threshold})" if threshold else ""
        lines.append(f"measured   {measured}{suffix}")
    return lines


def _finding_view(findings: list[dict], latest_action: dict[str, str]) -> list[dict]:
    """Findings sorted most-severe-first, decorated with UI-only fields."""
    view = []
    for f in sorted(findings, key=_finding_sort_key):
        state = latest_action.get(f.get("check_id", ""))
        conf = float(f.get("confidence", 0.0))
        view.append({
            **f,
            "conf_px": max(0, min(_CONF_BAR_PX, round(conf * _CONF_BAR_PX))),
            "evidence_lines": _evidence_lines(f.get("evidence") or {}),
            "state": state,
            "dismissed": state == "dismiss",
        })
    return view


def _pages_view(story: dict) -> list[dict]:
    """Media-rail entries for every page, defensive against junk feeds."""
    pages = []
    for page in story.get("pages") or []:
        if not isinstance(page, dict):
            continue
        action = page.get("action")
        cta = action.get("cta") if isinstance(action, dict) else ""
        pages.append({
            "page_id": page.get("page_id") or "?",
            "type": page.get("type") or "unknown",
            "asset_url": page.get("asset_url") or "",
            "cta": cta or "",
        })
    return pages


def _trend_points(scores: list[int], width: float = 600.0, height: float = 120.0,
                  pad: float = 8.0) -> str:
    """SVG polyline points for the score trend (viewBox 600x120, score 0-100).
    No chart library per the 06 spec — a polyline is all a sparkline needs."""
    if not scores:
        return ""
    if len(scores) == 1:
        scores = [scores[0], scores[0]]    # a single verdict still draws a line
    step = (width - 2 * pad) / (len(scores) - 1)
    points = []
    for i, raw in enumerate(scores):
        score = max(0, min(100, int(raw)))
        x = pad + i * step
        y = pad + (100 - score) * (height - 2 * pad) / 100.0
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _md_segments(text: str) -> list[dict]:
    """Split markdown into pipe-tables and everything-else, without a
    markdown lib: consecutive ``|``-prefixed lines become a table (separator
    rows dropped, first row is the header), all other lines go into <pre>."""
    segments: list[dict] = []
    prose: list[str] = []
    table: list[list[str]] = []

    def flush_prose() -> None:
        if prose and any(line.strip() for line in prose):
            segments.append({"kind": "pre", "text": "\n".join(prose).strip("\n")})
        prose.clear()

    def flush_table() -> None:
        if table:
            segments.append({"kind": "table", "rows": list(table)})
        table.clear()

    for line in text.splitlines():
        if line.strip().startswith("|"):
            flush_prose()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(c and set(c) <= set("-: ") for c in cells):
                continue                   # |---|---| separator row
            table.append(cells)
        else:
            flush_table()
            prose.append(line)
    flush_prose()
    flush_table()
    return segments


def _cache_stats(raw: str | None) -> dict | None:
    """Parse the engine's meta ``cache_stats`` JSON; None if absent/garbled."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    hits = int(data.get("replay_hits") or 0)
    misses = int(data.get("replay_misses") or 0)
    denom = hits + misses
    return {
        "replay_hits": hits,
        "replay_misses": misses,
        "live_calls": int(data.get("live_calls") or 0),
        "hit_rate": f"{100.0 * hits / denom:.0f}%" if denom else "-",
    }


# -- app factory -------------------------------------------------------------

def create_app(store: Store, settings: dict,
               tenants: dict[str, TenantPolicy]) -> FastAPI:
    """Build the dashboard app. The caller owns the store and runs uvicorn."""
    package_dir = Path(__file__).resolve().parent
    app = FastAPI(title="Greenlight", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(package_dir / "static")),
              name="static")
    templates = Jinja2Templates(directory=str(package_dir / "templates"))
    templates.env.filters["reltime"] = _reltime
    templates.env.filters["money"] = _money

    tenant_names = {tid: (t.tenant_name or tid) for tid, t in tenants.items()}

    def base_context(request: Request) -> dict:
        """Header data every page needs: today's counters + watcher pulse.
        The pulse goes gray after 3 missed poll intervals (60s at the
        default 20s ``feed.poll_interval_s`` cadence)."""
        counters = store.counters_today()
        stale_after = 3 * float(settings.get("feed", {}).get("poll_interval_s", 20))
        last = _parse_iso(counters.get("last_poll"))
        stale = last is None or (utcnow() - last).total_seconds() > stale_after
        return {"request": request, "counters": counters, "watcher_stale": stale}

    def tenant_tabs() -> list[tuple[str, str]]:
        """Configured tenants first, then any extra tenant ids in the DB."""
        tabs = [(tid, tenant_names[tid]) for tid in tenants]
        known = set(tenants)
        tabs.extend((tid, tid) for tid in store.tenant_ids() if tid not in known)
        return tabs

    def queue_context(request: Request, tenant: str | None, partial: bool) -> dict:
        ctx = base_context(request)
        rows = store.latest_verdicts(tenant_id=tenant or None)
        ctx.update({
            "rows": _queue_view(rows, tenant_names),
            "tabs": tenant_tabs(),
            "active_tenant": tenant or "",
            "partial": partial,
        })
        return ctx

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, tenant: str | None = None):
        """Triage queue, optionally filtered to one tenant tab."""
        return templates.TemplateResponse(
            request, "index.html", queue_context(request, tenant, partial=False))

    @app.get("/partials/queue", response_class=HTMLResponse)
    def queue_partial(request: Request, tenant: str | None = None):
        """Queue + header-counters fragment; app.js swaps it in every 8s."""
        return templates.TemplateResponse(
            request, "index.html", queue_context(request, tenant, partial=True))

    @app.get("/story/{story_id}", response_class=HTMLResponse)
    def story_page(request: Request, story_id: str):
        """Case file: media rail left, verdict + evidence-backed findings right."""
        row = store.get_verdict(story_id)
        if row is None:
            return HTMLResponse("<h1>story not found</h1>", status_code=404)
        findings = row.get("findings") or []
        eff_verdict, eff_score, latest_action = _effective_review(
            findings, store.get_feedback(story_id), settings)
        story = row.get("story") or {}
        ctx = base_context(request)
        ctx.update({
            "story_id": row["story_id"],
            "story_title": story.get("story_title") or row["story_id"],
            "tenant_id": row["tenant_id"],
            "tenant_name": tenant_names.get(row["tenant_id"], row["tenant_id"]),
            "verdict": row["verdict"],
            "score": row["score"],
            "effective_verdict": eff_verdict,
            "effective_score": eff_score,
            "checked_at": row["checked_at"],
            "cost_usd": row["cost_usd"],
            "pipeline_version": row["pipeline_version"],
            "escalated_by": row.get("escalated_by"),
            "headline": row.get("headline") or _headline(findings),
            "findings": _finding_view(findings, latest_action),
            "pages": _pages_view(story),
            "story_pretty": json.dumps(story, indent=2, ensure_ascii=False),
        })
        return templates.TemplateResponse(request, "story.html", ctx)

    @app.post("/feedback")
    async def feedback(request: Request):
        """Reviewer verdict on one finding; accepts JSON or form bodies.

        Urlencoded bodies are parsed with ``parse_qs`` directly — Starlette's
        ``request.form()`` needs python-multipart, which this offline demo
        deliberately does without.
        """
        try:
            if "json" in request.headers.get("content-type", ""):
                data = await request.json()
            else:
                raw = (await request.body()).decode("utf-8", "replace")
                data = {k: v[0] for k, v in
                        parse_qs(raw, keep_blank_values=True).items()}
        except Exception:
            return JSONResponse({"ok": False, "error": "unreadable body"},
                                status_code=400)
        story_id = str(data.get("story_id") or "").strip()
        check_id = str(data.get("check_id") or "").strip()
        action = str(data.get("action") or "").strip()
        if not story_id or not check_id or action not in ("confirm", "dismiss"):
            return JSONResponse(
                {"ok": False,
                 "error": "story_id, check_id and action (confirm|dismiss) required"},
                status_code=400)
        store.save_feedback(story_id, check_id, action)
        return {"ok": True}

    @app.get("/tenant/{tenant_id}", response_class=HTMLResponse)
    def tenant_page(request: Request, tenant_id: str):
        """Tenant health: score trend, pass rate, failing checks, cost."""
        health = store.tenant_health(tenant_id)
        max_count = max((n for _, n in health["top_checks"]), default=0)
        top_checks = [
            {"check_id": cid, "count": n,
             "pct": round(100.0 * n / max_count) if max_count else 0}
            for cid, n in health["top_checks"]
        ]
        policy = tenants.get(tenant_id)
        if policy is None:
            burn_in_label = "unknown (no policy pack)"
        elif policy.burn_in:
            burn_in_label = "active (full-tier)"
        else:
            burn_in_label = "off"
        ctx = base_context(request)
        ctx.update({
            "health": health,
            "tenant_name": tenant_names.get(tenant_id, tenant_id),
            "trend_points": _trend_points(health["scores"]),
            "top_checks": top_checks,
            "burn_in_label": burn_in_label,
        })
        return templates.TemplateResponse(request, "tenant.html", ctx)

    @app.get("/metrics", response_class=HTMLResponse)
    def metrics_page(request: Request):
        """Proof page: eval report, feedback stats, totals, cache hit rate."""
        segments = None
        for candidate in (Path("eval/report.md"),
                          package_dir.parent.parent / "eval" / "report.md"):
            if candidate.is_file():
                segments = _md_segments(candidate.read_text(encoding="utf-8"))
                break
        rows = store.latest_verdicts(limit=1_000_000)
        totals = {
            "stories": len(rows),
            "green": sum(1 for r in rows if r["verdict"] == "GREEN"),
            "amber": sum(1 for r in rows if r["verdict"] == "AMBER"),
            "red": sum(1 for r in rows if r["verdict"] == "RED"),
            "cost_usd": sum(r["cost_usd"] for r in rows),
        }
        fb_stats = [
            {**s, "auto_quiet": s["dismiss_rate"] >= _AUTO_QUIET_DISMISS_RATE}
            for s in store.feedback_stats()
        ]
        ctx = base_context(request)
        ctx.update({
            "segments": segments,
            "totals": totals,
            "fb_stats": fb_stats,
            "cache": _cache_stats(store.get_meta("cache_stats")),
        })
        return templates.TemplateResponse(request, "metrics.html", ctx)

    return app
