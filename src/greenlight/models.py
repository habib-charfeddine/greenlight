"""Pydantic data contracts for Greenlight.

Feed/Story/Page field names mirror ``fixtures/example_from_brief.json`` exactly —
the brief's schema is the contract and is never renamed. Feed-side models are
deliberately lenient (missing fields default, unknown extra fields are allowed):
a defective story must be INGESTED and FLAGGED by ``T0.schema_valid``, never
rejected at the parser.

Finding/StoryVerdict shapes come from 02_BUILD_SPEC.md and are stable API.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["P0", "P1", "P2", "P3"]
Verdict = Literal["GREEN", "AMBER", "RED"]

#: Lower index = more severe. Used for downgrades and sorting.
SEVERITY_ORDER: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


class _Lenient(BaseModel):
    """Base for feed-side models: tolerate junk, let checks do the judging."""

    model_config = ConfigDict(extra="allow")


class Action(_Lenient):
    cta: str = ""
    url: str = ""


class Page(_Lenient):
    page_id: str = ""
    type: str = ""  # "image" | "video" per the brief; anything else is a schema finding
    asset_url: str = ""
    action: Action | None = None


class StoryContext(_Lenient):
    categories: list[str] = Field(default_factory=list)
    tenant: str | None = None
    publish_date: str | None = None


class Story(_Lenient):
    story_id: str = ""
    story_title: str = ""
    pages: list[Page] = Field(default_factory=list)
    context: StoryContext | None = None


class Feed(_Lenient):
    tenant_id: str = ""
    tenant_name: str = ""
    last_synced_at: str | None = None
    stories: list[Story] = Field(default_factory=list)


class Evidence(BaseModel):
    json_path: str | None = None      # e.g. "stories[0].pages[1].action.url"
    url: str | None = None
    http_status: int | None = None
    frame_ts: float | None = None     # seconds into video
    excerpt: str | None = None        # quoted text evidence (<=200 chars)
    measured: str | None = None       # e.g. "laplacian_var=41.2"
    threshold: str | None = None      # e.g. "min=100"


class Finding(BaseModel):
    check_id: str                     # from 03_CHECK_CATALOG.md, e.g. "T0.cta_link_health"
    severity: Severity
    confidence: float                 # deterministic checks = 1.0
    summary: str                      # one sentence, human-readable
    evidence: Evidence
    suggested_fix: str | None = None
    tier: int
    model: str | None = None          # AI findings only
    policy_version: str
    prompt_version: str | None = None
    cost_usd: float = 0.0
    latency_ms: int = 0


class StoryVerdict(BaseModel):
    story_id: str
    tenant_id: str
    content_hash: str                 # sha256 of canonicalized story JSON
    verdict: Verdict
    score: int                        # 0-100, for trends only (verdict drives action)
    findings: list[Finding]
    checked_at: datetime
    pipeline_version: str
    totals: dict                      # {"cost_usd", "latency_ms", "checks_run", "checks_skipped"}


def canonical_json(obj: Any) -> str:
    """Canonical JSON: sorted keys, compact separators — byte-stable across runs."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(story: Story) -> str:
    """sha256 of the canonicalized story JSON (engine rule #1: delta detection)."""
    payload = story.model_dump(mode="json")
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
