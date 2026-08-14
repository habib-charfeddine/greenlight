"""Seed fixtures/ai_replay.jsonl with HAND-AUTHORED judge responses.

Why this exists (and why it's committed): replay mode must demonstrate the full
pipeline — including the brief fixture's swapped-CTA case — with zero API keys.
Real model outputs don't exist until someone runs --live, so this script writes
plausible judge responses derived from the golden labels, each marked
``"source": "hand_authored"``. They demonstrate PIPELINE PLUMBING, not model
skill; the eval report says so explicitly, and a --live run replaces them with
recorded responses (this script never overwrites a ``source: live`` entry).

Usage (repo root):
    python scripts/seed_replay.py            # brief fixture only
    python scripts/seed_replay.py --world    # + eval world (seed 1337)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # repo root (feedgen)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # greenlight (no install needed)

from greenlight.llm import replay_key  # noqa: E402
from greenlight.models import Feed, Story, content_hash, utcnow  # noqa: E402
from greenlight.policy import load_settings  # noqa: E402

REPLAY_PATH = Path("fixtures/ai_replay.jsonl")

# Must match eval/run_eval.py defaults exactly — asset URLs embed the port, so
# the port participates in every story's content hash.
EVAL_SEED, EVAL_STORIES, EVAL_PORT = 1337, 40, 8199

_PAGE_RE = re.compile(r"pages\[(\d+)\]")


def _entry(key: str, model: str, prompt_version: str, story: Story,
           response: dict) -> dict:
    return {
        "key": key, "model": model, "prompt_version": prompt_version,
        "content_hash": content_hash(story), "story_id": story.story_id,
        "source": "hand_authored", "response": response,
        "usage": None, "cost_usd": 0.0, "latency_ms": 0,
        "recorded_at": utcnow().isoformat(),
        "note": "authored by scripts/seed_replay.py from golden labels — "
                "demonstrates pipeline plumbing, not model skill",
    }


def _action_for(story: Story, json_path: str) -> tuple[str, str]:
    m = _PAGE_RE.search(json_path or "")
    if m:
        i = int(m.group(1))
        if 0 <= i < len(story.pages) and story.pages[i].action:
            a = story.pages[i].action
            return a.cta, a.url
    return "", ""


def _t1_finding(check_id: str, story: Story, json_path: str) -> dict:
    """Plausible judge finding for a seeded defect. Deterministic, no RNG."""
    title = story.story_title[:180]
    if check_id == "T1.cta_copy_mismatch":
        cta, url = _action_for(story, json_path)
        return {"check_id": check_id, "severity": "P1", "confidence": 0.96,
                "summary": f"CTA text '{cta}' does not match its destination path",
                "json_path": json_path, "evidence_excerpt": f"{cta} -> {url}"[:200],
                "suggested_fix": "Point the CTA at a destination matching its text, "
                                 "or swap the mismatched CTAs"}
    if check_id == "T1.spelling_grammar":
        return {"check_id": check_id, "severity": "P1", "confidence": 0.97,
                "summary": "Misspellings in the story title",
                "json_path": json_path or "story_title", "evidence_excerpt": title,
                "suggested_fix": "Correct the spelling of club names and common words"}
    if check_id == "T1.tone_style":
        return {"check_id": check_id, "severity": "P2", "confidence": 0.88,
                "summary": "Title violates tenant tone rules (clickbait/ALL-CAPS phrasing)",
                "json_path": json_path or "story_title", "evidence_excerpt": title}
    if check_id == "T1.coherence":
        return {"check_id": check_id, "severity": "P1", "confidence": 0.85,
                "summary": "Title names a club that does not appear in the story's categories",
                "json_path": json_path or "story_title", "evidence_excerpt": title,
                "suggested_fix": "Align the title with the fixture in context.categories"}
    if check_id == "T1.safety_screen":
        return {"check_id": check_id, "severity": "P0", "confidence": 0.95,
                "summary": "Threatening or explicit phrase in copy",
                "json_path": json_path or "story_title", "evidence_excerpt": title,
                "suggested_fix": "Remove the offending phrase before this reaches fans"}
    if check_id == "T1.injection_attempt":
        return {"check_id": check_id, "severity": "P1", "confidence": 0.98,
                "summary": "Content contains an instruction aimed at the reviewing system",
                "json_path": json_path or "story_title", "evidence_excerpt": title,
                "suggested_fix": "Strip the embedded instruction; investigate the source"}
    raise ValueError(f"no hand-authored template for {check_id}")


def _t2_finding(check_id: str, story: Story, json_path: str) -> dict:
    if check_id == "T2.visual_text":
        return {"check_id": check_id, "severity": "P1", "confidence": 0.93,
                "summary": "Burned-in text contains a misspelling and a placeholder "
                           "('UNTIED' for United, 'TDB' for TBD)",
                "json_path": json_path, "frame_ts": 0.0,
                "evidence_excerpt": "SEALS UNTIED — TICKETS TDB",
                "suggested_fix": "Re-render the graphic with corrected text"}
    if check_id == "T2.thumb_title_match":
        return {"check_id": check_id, "severity": "P2", "confidence": 0.82,
                "summary": "Pictured content does not match the title's claim",
                "json_path": json_path, "frame_ts": 0.0,
                "evidence_excerpt": story.story_title[:200]}
    raise ValueError(f"no hand-authored template for {check_id}")


def _headline(findings: list[dict]) -> str:
    if not findings:
        return ""
    worst = sorted(findings, key=lambda f: f["severity"])[0]
    more = f" (+{len(findings) - 1} more)" if len(findings) > 1 else ""
    return f"{worst['summary']}{more}."


def brief_fixture_entries(settings: dict) -> list[dict]:
    """The brief's own sample data: story_123 carries swapped CTAs."""
    feed = Feed.model_validate(json.loads(
        Path("fixtures/example_from_brief.json").read_text(encoding="utf-8")))
    t1 = settings["models"]["tier1"]
    entries = []
    for story in feed.stories:
        findings: list[dict] = []
        if story.story_id == "story_123":
            findings = [_t1_finding("T1.cta_copy_mismatch", story, f"pages[{i}].action.url")
                        for i, p in enumerate(story.pages)]
            # The specific fix a reviewer needs to see, verbatim:
            findings[0]["summary"] = "CTA 'Watch highlights' links to /match-report"
            findings[1]["summary"] = "CTA 'Buy tickets' links to /highlights"
            findings[0]["suggested_fix"] = ("Swap the two CTA targets — or point "
                                            "'Watch highlights' at /highlights")
            findings[1]["suggested_fix"] = ("Point 'Buy tickets' at a /tickets page — "
                                            "the two CTAs appear swapped")
        response = {"findings": findings, "abstain": False,
                    "headline": "Both CTAs point at swapped destinations — 'Buy tickets' "
                                "opens highlights, 'Watch highlights' opens the match report."
                                if findings else ""}
        key = replay_key(t1["model"], t1["prompt_version"], content_hash(story))
        entries.append(_entry(key, t1["model"], t1["prompt_version"], story, response))
    return entries


def eval_world_entries(settings: dict) -> list[dict]:
    """T1 entry for EVERY eval-world story; T2 entries where they can fire."""
    from feedgen.generate import generate_world
    from greenlight.engine import _sample_from_hash

    with tempfile.TemporaryDirectory(prefix="greenlight-seed-") as td:
        world = generate_world(Path(td), seed=EVAL_SEED, stories=EVAL_STORIES,
                               tenants=2, defect_rate=0.35, port=EVAL_PORT)
        feeds = json.loads(Path(world.feed_path).read_text(encoding="utf-8"))
        labels = json.loads(Path(world.labels_path).read_text(encoding="utf-8"))

    t1, t2 = settings["models"]["tier1"], settings["models"]["tier2"]
    rate = float(settings.get("vision_sample_rate", 0.15))
    entries = []
    for feed_raw in feeds:
        for story in Feed.model_validate(feed_raw).stories:
            story_labels = labels.get(story.story_id, [])
            h = content_hash(story)

            t1_findings = [_t1_finding(l["check_id"], story, l.get("json_path", ""))
                           for l in story_labels if l["check_id"].startswith("T1.")]
            entries.append(_entry(
                replay_key(t1["model"], t1["prompt_version"], h),
                t1["model"], t1["prompt_version"], story,
                {"findings": t1_findings, "abstain": False,
                 "headline": _headline(t1_findings)}))

            t2_labels = [l for l in story_labels if l["check_id"].startswith("T2.")]
            # T2 runs on escalation (any label implies a likely P0/P1) or on the
            # deterministic hash sample — cover both so no story hits a replay miss.
            if story_labels or _sample_from_hash(h, rate):
                t2_findings = [_t2_finding(l["check_id"], story, l.get("json_path", ""))
                               for l in t2_labels]
                entries.append(_entry(
                    replay_key(t2["model"], t2["prompt_version"], h),
                    t2["model"], t2["prompt_version"], story,
                    {"findings": t2_findings, "abstain": False}))
    return entries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", action="store_true",
                    help="also seed the eval world (requires feedgen module)")
    args = ap.parse_args()

    settings = load_settings()
    existing: dict[str, dict] = {}
    if REPLAY_PATH.exists():
        for line in REPLAY_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                e = json.loads(line)
                existing[e["key"]] = e

    fresh = brief_fixture_entries(settings)
    if args.world:
        fresh += eval_world_entries(settings)

    added = kept_live = replaced = 0
    for e in fresh:
        cur = existing.get(e["key"])
        if cur is not None and cur.get("source") == "live":
            kept_live += 1          # never clobber a real recorded response
            continue
        if cur is not None:
            replaced += 1
        else:
            added += 1
        existing[e["key"]] = e

    REPLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPLAY_PATH, "w", encoding="utf-8") as f:
        for e in existing.values():
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"replay cache: {added} added, {replaced} refreshed, "
          f"{kept_live} live entries preserved, total {len(existing)}")


if __name__ == "__main__":
    main()
