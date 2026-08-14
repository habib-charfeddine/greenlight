"""The 05_DATA_AND_EVAL edge-case list: the pipeline must survive all of it."""
import json

import pytest

from greenlight.engine import Engine
from greenlight.models import Feed, Story
from greenlight.policy import load_tenants
from greenlight.store import Store

from .conftest import REPO, make_story


@pytest.fixture()
def engine(settings, tmp_path):
    s = dict(settings)
    s["replay_cache"] = str(tmp_path / "replay.jsonl")  # empty: judges degrade to P3
    store = Store(tmp_path / "t.db", tmp_path / "a.jsonl", tmp_path / "f.jsonl")
    eng = Engine(s, load_tenants(REPO / "config" / "tenants"), store, mode="replay")
    yield eng
    eng.close()


@pytest.fixture()
def afl(engine, settings):
    from greenlight.policy import EffectivePolicy
    return EffectivePolicy(settings,
                           load_tenants(REPO / "config" / "tenants")["tenant_antarctic_league_001"])


def test_empty_stories_array(engine):
    feed = Feed.model_validate({"tenant_id": "tenant_antarctic_league_001",
                                "tenant_name": "AFL", "last_synced_at": "2026-08-14T00:00:00Z",
                                "stories": []})
    assert engine.process_feed(feed) == []


def test_story_with_zero_pages_flagged_not_fatal(engine, afl):
    v = engine.process_story(make_story(story_id="nopages", pages=[]), afl)
    assert v is not None
    assert any(f.check_id == "T0.schema_valid" and f.evidence.json_path == "pages"
               for f in v.findings)
    assert v.verdict == "AMBER"  # missing pages is critical (P1)


def test_missing_context_flagged_p1(engine, afl):
    story = Story.model_validate({
        "story_id": "noctx", "story_title": "Match report: Penguin FC 2-1 Seals United",
        "pages": [{"page_id": "p1", "type": "image",
                   "asset_url": "http://localhost:8100/assets/noctx/p1.jpg",
                   "action": {"cta": "Read more",
                              "url": "http://localhost:8100/site/match-report"}}]})
    v = engine.process_story(story, afl)
    ctx_findings = [f for f in v.findings
                    if f.check_id == "T0.schema_valid" and f.evidence.json_path == "context"]
    assert ctx_findings and ctx_findings[0].severity == "P1"


def test_duplicate_page_ids_are_minor(engine, afl):
    story = make_story(story_id="dupids", pages=[
        {"page_id": "p1", "type": "image",
         "asset_url": "http://localhost:59999/a.jpg",
         "action": {"cta": "Read match report", "url": "http://localhost:59999/site/x"}},
        {"page_id": "p1", "type": "image",
         "asset_url": "http://localhost:59999/b.jpg",
         "action": {"cta": "Read match report", "url": "http://localhost:59999/site/x"}},
    ])
    v = engine.process_story(story, afl)
    dups = [f for f in v.findings if "duplicate page_id" in f.summary]
    assert dups and dups[0].severity == "P3"


def test_asset_server_down_means_unverifiable_and_run_completes(engine, afl):
    # localhost:59999 has nothing listening -> connection refused -> infra, not
    # content: UNVERIFIABLE P3, never a RED page.
    story = make_story(story_id="down", pages=[{
        "page_id": "p1", "type": "image",
        "asset_url": "http://localhost:59999/assets/x.jpg",
        "action": {"cta": "Read match report", "url": "http://localhost:59999/site/y"}}])
    v = engine.process_story(story, afl)
    assert v is not None
    link_findings = [f for f in v.findings
                     if f.check_id in ("T0.asset_reachable", "T0.cta_link_health")]
    assert link_findings and all(f.severity == "P3" for f in link_findings)


def test_ffmpeg_absent_skips_video_checks(engine, afl, monkeypatch, settings, tmp_path):
    from greenlight.checks import base as check_base
    from greenlight.checks import tier0_media
    import httpx

    ctx = check_base.CheckContext(
        policy=afl, settings=settings, http=httpx.Client(), store=None,
        offline=True, tmpdir=tmp_path,
        ffmpeg=check_base.FFmpegInfo(ffmpeg=None, ffprobe=None))
    story = make_story(story_id="vid", pages=[{
        "page_id": "p1", "type": "video",
        "asset_url": "http://localhost:59999/assets/vid/p1.mp4"}])
    findings = tier0_media.run(story, ctx)
    # No crash; anything emitted about the video is a skip note, not an alert.
    assert all(f.severity == "P3" for f in findings)


def test_malformed_feed_json_raises_feed_error_keeps_cursor(engine, tmp_path):
    from greenlight.ingest import FeedError
    engine.store.set_cursor("tenant_antarctic_league_001", "2026-08-13T00:00:00Z")
    bad = tmp_path / "bad.json"
    bad.write_text("{definitely not json", encoding="utf-8")
    with pytest.raises(FeedError):
        engine.process_feed_path(bad)
    assert engine.store.get_cursor("tenant_antarctic_league_001") == "2026-08-13T00:00:00Z"


def test_gate_mode_other_than_advisory_is_roadmap(engine, settings):
    from greenlight.policy import TenantPolicy
    engine.tenants["strict_t"] = TenantPolicy(
        tenant_id="strict_t", tenant_name="Strict", gate_mode="hold_red")
    feed = Feed.model_validate({"tenant_id": "strict_t", "tenant_name": "Strict",
                                "last_synced_at": None, "stories": []})
    with pytest.raises(NotImplementedError):
        engine.process_feed(feed)
