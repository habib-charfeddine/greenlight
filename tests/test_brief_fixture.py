"""THE acceptance test: the brief's own sample data through the full pipeline.

story_123's page_2 says "Buy tickets" but links to /highlights, and page_1 says
"Watch highlights" but links to /match-report — planted (we believe) by the
task authors. Greenlight must catch it, offline, with evidence.
"""
import pytest

from greenlight.engine import Engine
from greenlight.ingest import load_feeds
from greenlight.policy import load_tenants
from greenlight.store import Store

from .conftest import REPO


@pytest.fixture()
def engine(settings, tmp_path):
    # Uses the COMMITTED replay cache (fixtures/ai_replay.jsonl) — this is the
    # exact offline path a reviewer runs on a fresh clone.
    store = Store(tmp_path / "t.db", tmp_path / "a.jsonl", tmp_path / "f.jsonl")
    eng = Engine(settings, load_tenants(REPO / "config" / "tenants"), store,
                 mode="replay")
    yield eng
    eng.close()


def test_story_123_gets_amber_with_cta_mismatch_on_both_pages(engine):
    verdicts = engine.process_feed_path(REPO / "fixtures" / "example_from_brief.json")
    by_id = {v.story_id: v for v in verdicts}

    v = by_id["story_123"]
    assert v.verdict == "AMBER"
    mismatches = [f for f in v.findings if f.check_id == "T1.cta_copy_mismatch"]
    assert {f.evidence.json_path for f in mismatches} == {
        "pages[0].action.url", "pages[1].action.url"}
    assert all(f.severity == "P1" for f in mismatches)

    # The brief's asset/CTA hosts are unreachable from the sandbox: the
    # three-state logic reports UNVERIFIABLE as P3 info — it never alerts.
    unverifiable = [f for f in v.findings if f.check_id in
                    ("T0.asset_reachable", "T0.cta_link_health")]
    assert unverifiable and all(f.severity == "P3" for f in unverifiable)

    # story_124 is clean editorial content.
    assert by_id["story_124"].verdict == "GREEN"


def test_rerun_never_rejudges_unchanged_stories(engine):
    first = engine.process_feed_path(REPO / "fixtures" / "example_from_brief.json")
    second = engine.process_feed_path(REPO / "fixtures" / "example_from_brief.json")
    assert len(first) == 2
    assert second == []  # both skipped via (content_hash, policy, prompt) cache
