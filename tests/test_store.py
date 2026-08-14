"""Store behavior the dashboard and engine lean on."""
from greenlight.models import Evidence, Finding, StoryVerdict, utcnow
from greenlight.store import Store


def _store(tmp_path) -> Store:
    return Store(tmp_path / "s.db", tmp_path / "a.jsonl", tmp_path / "f.jsonl")


def _verdict(story_id="s1", verdict="AMBER", headline="fix the CTA") -> StoryVerdict:
    return StoryVerdict(
        story_id=story_id, tenant_id="t1", content_hash="h" * 64,
        verdict=verdict, score=85,
        findings=[Finding(check_id="T1.cta_copy_mismatch", severity="P1",
                          confidence=0.96, summary="mismatch",
                          evidence=Evidence(json_path="pages[0].action.url"),
                          tier=1, policy_version="afl-2026.08.1")],
        checked_at=utcnow(), pipeline_version="0.1.0",
        totals={"cost_usd": 0.002, "latency_ms": 120, "checks_run": 5,
                "checks_skipped": 0, "headline": headline},
    )


def test_verdict_roundtrip_with_headline_and_judged_marker(tmp_path):
    s = _store(tmp_path)
    s.save_verdict(_verdict(), story_json={"story_id": "s1"},
                   escalated_by="p0p1", prompt_version="pv")
    row = s.get_verdict("s1")
    assert row["verdict"] == "AMBER"
    assert row["headline"] == "fix the CTA"
    assert row["escalated_by"] == "p0p1"
    assert row["findings"][0]["check_id"] == "T1.cta_copy_mismatch"
    assert s.has_verdict("h" * 64, "afl-2026.08.1", "pv")
    assert not s.has_verdict("h" * 64, "afl-2026.08.1", "OTHER-prompt")
    assert not s.has_verdict("x" * 64, "afl-2026.08.1", "pv")


def test_upsert_keeps_one_row_per_story(tmp_path):
    s = _store(tmp_path)
    s.save_verdict(_verdict(verdict="AMBER"), {}, prompt_version="pv")
    s.save_verdict(_verdict(verdict="RED"), {}, prompt_version="pv")
    rows = s.latest_verdicts()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "RED"


def test_feedback_stats_and_dismiss_rate(tmp_path):
    s = _store(tmp_path)
    s.save_feedback("s1", "T0.image_blur", "dismiss")
    s.save_feedback("s2", "T0.image_blur", "dismiss")
    s.save_feedback("s3", "T0.image_blur", "confirm")
    s.save_feedback("s1", "T1.tone_style", "confirm")
    stats = {r["check_id"]: r for r in s.feedback_stats()}
    assert stats["T0.image_blur"]["dismisses"] == 2
    assert abs(stats["T0.image_blur"]["dismiss_rate"] - 2 / 3) < 1e-9
    assert stats["T1.tone_style"]["dismiss_rate"] == 0.0
    # feedback.jsonl mirrors every row (the audit trail for future auto-tuning)
    assert len((tmp_path / "f.jsonl").read_text().splitlines()) == 4


def test_cursor_and_asset_hash_memory(tmp_path):
    s = _store(tmp_path)
    assert s.get_cursor("t1") is None
    s.set_cursor("t1", "2026-08-14T10:00:00Z")
    assert s.get_cursor("t1") == "2026-08-14T10:00:00Z"
    s.add_asset_hash("t1", "http://x/a.jpg", "ff00", "s1")
    s.add_title("t1", "s1", "Matchday build-up")
    assert s.recent_asset_hashes("t1")[0]["phash"] == "ff00"
    assert s.recent_titles("t1")[0]["title"] == "Matchday build-up"
    assert s.recent_asset_hashes("other-tenant") == []


def test_tenant_health_aggregates(tmp_path):
    s = _store(tmp_path)
    s.save_verdict(_verdict("s1", "GREEN"), {}, prompt_version="pv")
    s.save_verdict(_verdict("s2", "AMBER"), {}, prompt_version="pv")
    h = s.tenant_health("t1")
    assert h["n"] == 2
    assert h["pass_rate"] == 0.5
    assert h["review_load"] == 0.5
    assert h["top_checks"][0][0] == "T1.cta_copy_mismatch"
