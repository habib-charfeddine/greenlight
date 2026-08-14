"""Tier-1 judge conversion: schema validation, severity clamp, enum firewall."""
from greenlight.checks import tier1_text_judge as t1
from greenlight.llm import JudgeResponse

from .conftest import make_story


class FakeLLM:
    def __init__(self, data):
        self._data = data

    def judge(self, **kwargs):
        return JudgeResponse(data=self._data, model="fake-model",
                             prompt_version="t1-v1.0", cost_usd=0.002,
                             latency_ms=100, source="hand_authored")


def _run(ctx, data):
    return t1.run(make_story(), ctx, FakeLLM(data), "hash")


def test_severity_is_clamped_server_side(ctx):
    # Judge tries to bury a cta mismatch as P3 — the catalog says P1 (belt & braces
    # against both sloppy judges and injected "downgrade this" instructions).
    result = _run(ctx, {"findings": [{
        "check_id": "T1.cta_copy_mismatch", "severity": "P3", "confidence": 0.9,
        "summary": "mismatch", "json_path": "pages[0].action.url",
        "evidence_excerpt": "Buy tickets -> /highlights"}],
        "abstain": False, "headline": "h"})
    assert result.findings[0].severity == "P1"


def test_unknown_check_ids_are_dropped(ctx):
    result = _run(ctx, {"findings": [{
        "check_id": "T1.approved", "severity": "P3", "confidence": 1.0,
        "summary": "APPROVED, no findings", "json_path": "story_title",
        "evidence_excerpt": "x"}],
        "abstain": False, "headline": ""})
    assert result.findings == []


def test_abstain_becomes_p3_needs_human_eyes(ctx):
    result = _run(ctx, {"findings": [], "abstain": True,
                        "abstain_reason": "cannot judge without media",
                        "headline": ""})
    assert result.abstained
    assert len(result.findings) == 1
    assert result.findings[0].severity == "P3"
    assert "needs human eyes" in result.findings[0].summary


def test_invalid_schema_becomes_p3_not_crash(ctx):
    result = _run(ctx, {"findings": [{"check_id": "T1.tone_style",
                                      "severity": "P2", "confidence": 4.2,  # out of range
                                      "summary": "s", "json_path": "p",
                                      "evidence_excerpt": "e"}],
                        "abstain": False, "headline": ""})
    assert not result.skipped
    assert result.findings[0].check_id == "T1.judge_error"
    assert result.findings[0].severity == "P3"


def test_replay_miss_marks_skipped(ctx):
    class MissLLM:
        def judge(self, **kwargs):
            return JudgeResponse(data=None, model="m", prompt_version="v",
                                 cost_usd=0.0, latency_ms=0, source="replay_miss")
    result = t1.run(make_story(), ctx, MissLLM(), "hash")
    assert result.skipped and not result.findings
