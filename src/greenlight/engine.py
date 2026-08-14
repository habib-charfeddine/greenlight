"""Orchestrator: tiers, escalation, verdict synthesis, routing.

Implements the nine engine rules from 02_BUILD_SPEC.md. Verdict synthesis is
deterministic code, not an LLM.
"""
from __future__ import annotations

import logging
import time
from typing import Literal

import httpx

from .checks import base as check_base
from .checks import tier1_text_judge, tier2_vision_judge
from .checks.base import CheckContext, guard, make_finding
from .ingest import ensure_story_id, load_feeds
from .llm import LLMClient
from .models import Feed, Finding, SEVERITY_ORDER, Story, StoryVerdict, content_hash, utcnow
from .policy import EffectivePolicy, TenantPolicy, apply_overrides
from .store import Store
from .webhook import send_red_alert

log = logging.getLogger("greenlight")

_DOWNGRADE = {"P0": "P1", "P1": "P2", "P2": "P3", "P3": "P3"}


def effective_severity(f: Finding, downgrade_below: float) -> str:
    """AI findings below the confidence bar drop one severity level (rule 5).

    False positives are the death of QA tools — a shaky P1 becomes a P2 note
    instead of a review-queue item. Deterministic checks (tier 0) never downgrade.
    """
    if f.tier >= 1 and f.confidence < downgrade_below:
        return _DOWNGRADE[f.severity]
    return f.severity


def synthesize(findings: list[Finding], settings: dict) -> tuple[str, int]:
    """RED if any effective P0; AMBER if any effective P1; else GREEN.

    score = max(0, 100 - sum(weights)) — a trend signal only; the verdict
    drives action.
    """
    downgrade_below = float(settings.get("confidence_downgrade_below", 0.70))
    weights = settings.get("severity_weights", {"P0": 40, "P1": 15, "P2": 5, "P3": 1})
    effective = [effective_severity(f, downgrade_below) for f in findings]
    verdict = "GREEN"
    if any(s == "P0" for s in effective):
        verdict = "RED"
    elif any(s == "P1" for s in effective):
        verdict = "AMBER"
    score = max(0, 100 - sum(int(weights.get(s, 0)) for s in effective))
    return verdict, score


def _sample_from_hash(story_hash: str, rate: float) -> bool:
    """Deterministic Bernoulli draw from the content hash.

    Same story -> same decision on every run, so replay caches line up and
    eval runs are reproducible without seeding global RNG state.
    """
    return (int(story_hash[:8], 16) / 0xFFFFFFFF) < rate


class Engine:
    def __init__(self, settings: dict, tenants: dict[str, TenantPolicy],
                 store: Store, mode: Literal["replay", "live"] = "replay"):
        self.settings = settings
        self.tenants = tenants
        self.store = store
        self.mode = mode
        self.llm = LLMClient(settings, mode=mode)
        self.http = httpx.Client(follow_redirects=True)
        # T1 + T2 prompt versions together key the "already judged?" check:
        # bumping either re-judges everything (rule 1).
        self.prompt_version = (
            f'{settings["models"]["tier1"]["prompt_version"]}'
            f'+{settings["models"]["tier2"]["prompt_version"]}'
        )

    # -- public entry points ------------------------------------------------

    def process_feed_path(self, source) -> list[StoryVerdict]:
        feeds = load_feeds(source, self.http)
        verdicts: list[StoryVerdict] = []
        for feed in feeds:
            verdicts.extend(self.process_feed(feed))
        return verdicts

    def process_feed(self, feed: Feed) -> list[StoryVerdict]:
        policy = self._policy_for(feed)
        if policy.tenant.gate_mode != "advisory":
            raise NotImplementedError(
                f"gate_mode '{policy.tenant.gate_mode}' is a roadmap item — v1 is "
                "advisory-only by design (Greenlight never blocks publishing; "
                "flip to blocking only once precision is proven on your feed)."
            )
        verdicts: list[StoryVerdict] = []
        new = skipped = 0
        for i, story in enumerate(feed.stories):
            story = ensure_story_id(story, i)
            v = self.process_story(story, policy, story_index=i)
            if v is None:
                skipped += 1
            else:
                new += 1
                verdicts.append(v)
        if feed.last_synced_at:
            self.store.set_cursor(policy.tenant_id, feed.last_synced_at)
        self.store.set_last_poll()
        self.store.set_meta("cache_stats", __import__("json").dumps(self.llm.stats))
        log.info("feed %s: %d new/changed judged, %d unchanged skipped (cache hits)",
                 feed.tenant_id, new, skipped)
        return verdicts

    # -- per-story pipeline -------------------------------------------------

    def process_story(self, story: Story, policy: EffectivePolicy,
                      story_index: int = 0) -> StoryVerdict | None:
        story_hash = content_hash(story)
        if self.store.has_verdict(story_hash, policy.policy_version, self.prompt_version):
            log.info("skip %s: unchanged (hash %s..., policy %s) — cached verdict",
                     story.story_id, story_hash[:8], policy.policy_version)
            return None

        t_start = time.monotonic()
        ctx = CheckContext(
            policy=policy, settings=self.settings, http=self.http,
            store=self.store, offline=(self.mode == "replay"),
            story_index=story_index,
        )

        findings: list[Finding] = []
        checks_run = 0
        checks_skipped = 0

        # Tier 0 — always, each module guarded (rule 2)
        for name, module in self._tier0_modules():
            findings.extend(guard(name, module.run, story, ctx))
            checks_run += 1

        # Tier 1 — every new/changed story (rule 3)
        t1 = tier1_text_judge.run(story, ctx, self.llm, story_hash)
        findings.extend(t1.findings)
        if t1.skipped:
            checks_skipped += 1
            findings.append(make_finding(
                ctx, "T1.abstain",
                f"text judge unavailable ({t1.source}) — needs human eyes",
                severity="P3", tier=1))
        else:
            checks_run += 1

        # Tier 2 — escalation / sample / burn-in (rule 4)
        escalated_by = self._tier2_trigger(findings, story_hash, policy)
        t2_cost = 0.0
        t2_latency = 0
        if escalated_by:
            t2 = tier2_vision_judge.run(story, ctx, self.llm, story_hash)
            findings.extend(t2.findings)
            t2_cost, t2_latency = t2.cost_usd, t2.latency_ms
            if t2.skipped:
                checks_skipped += 1
            else:
                checks_run += 1

        checks_skipped += sum(1 for f in findings if f.summary.startswith("skipped:"))

        # Per-tenant kill switch + severity overrides, then synthesis (rules 5)
        findings = apply_overrides(findings, policy)
        verdict, score = synthesize(findings, self.settings)

        latency_ms = int((time.monotonic() - t_start) * 1000)
        totals = {
            "cost_usd": round(t1.cost_usd + t2_cost, 6),
            "latency_ms": latency_ms,
            "checks_run": checks_run,
            "checks_skipped": checks_skipped,
            "headline": t1.headline or "",
        }
        v = StoryVerdict(
            story_id=story.story_id,
            tenant_id=policy.tenant_id,
            content_hash=story_hash,
            verdict=verdict, score=score,
            findings=sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity]),
            checked_at=utcnow(),
            pipeline_version=str(self.settings.get("pipeline_version", "0.0.0")),
            totals=totals,
        )
        self.store.save_verdict(v, story_json=story.model_dump(mode="json"),
                                escalated_by=escalated_by,
                                prompt_version=self.prompt_version)

        # Routing (rule 6): RED alerts now; AMBER waits in the queue; GREEN is silent.
        if verdict == "RED":
            send_red_alert(v, story.story_title, self.settings)
        log.info("%s %s score=%d findings=%d cost=$%.4f %s",
                 verdict, story.story_id, score, len(findings),
                 totals["cost_usd"], f"escalated={escalated_by}" if escalated_by else "")
        return v

    # -- helpers ------------------------------------------------------------

    def _tier0_modules(self):
        from .checks import tier0_dupes, tier0_links, tier0_media, tier0_schema
        return [("tier0_schema", tier0_schema), ("tier0_links", tier0_links),
                ("tier0_media", tier0_media), ("tier0_dupes", tier0_dupes)]

    def _tier2_trigger(self, findings: list[Finding], story_hash: str,
                       policy: EffectivePolicy) -> str | None:
        if any(f.severity in ("P0", "P1") for f in findings):
            return "p0p1"
        if policy.tenant.burn_in:
            seen = len(self.store.latest_verdicts(
                policy.tenant_id, int(self.settings.get("burn_in_stories", 20))))
            if seen < int(self.settings.get("burn_in_stories", 20)):
                return "burn_in"
        if _sample_from_hash(story_hash, policy.vision_sample_rate):
            return "sample"
        return None

    def _policy_for(self, feed: Feed) -> EffectivePolicy:
        tenant = self.tenants.get(feed.tenant_id)
        if tenant is None:
            log.warning("unknown tenant %s — using conservative defaults", feed.tenant_id)
            tenant = TenantPolicy(
                tenant_id=feed.tenant_id or "unknown",
                tenant_name=feed.tenant_name or feed.tenant_id or "unknown",
                policy_version="default-2026.08",
            )
        return EffectivePolicy(self.settings, tenant)

    def close(self) -> None:
        self.http.close()
