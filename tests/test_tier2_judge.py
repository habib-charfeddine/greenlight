"""Tier-2 vision judge: skip paths, severity clamp, frame evidence."""
from greenlight.checks import tier2_vision_judge as t2
from greenlight.llm import JudgeResponse

from .conftest import make_story


class FakeLLM:
    def __init__(self, data):
        self._data = data
        self.called = False

    def judge(self, **kwargs):
        self.called = True
        return JudgeResponse(data=self._data, model="fake-sonnet",
                             prompt_version="t2-v1.0", cost_usd=0.014,
                             latency_ms=900, source="hand_authored")


def test_no_fetchable_images_skips_without_calling_the_model(ctx):
    # External asset in offline mode -> UNVERIFIABLE -> no images -> no call.
    story = make_story(pages=[{
        "page_id": "p1", "type": "image",
        "asset_url": "https://cdn.example.com/x.jpg",
        "action": {"cta": "Read", "url": "http://localhost:8100/site/x"}}])
    llm = FakeLLM({"findings": [], "abstain": False})
    result = t2.run(story, ctx, llm, "hash")
    assert result.skipped
    assert not llm.called  # a vision call with zero images would be pure waste


def _run_with_image(ctx, tmp_path, data):
    """Story whose 'asset' is served from a local file via a data-free path:
    monkey-approach — pre-populate the ctx bytes cache with a real JPEG."""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (540, 960), (30, 60, 90)).save(buf, format="JPEG")
    url = "http://localhost:8100/assets/s/p.jpg"
    from greenlight.checks.base import FetchResult
    ctx._bytes_cache[url] = FetchResult("OK", 200, "image/jpeg", content=buf.getvalue())
    story = make_story(pages=[{"page_id": "p1", "type": "image", "asset_url": url,
                               "action": {"cta": "Read",
                                          "url": "http://localhost:8100/site/x"}}])
    return t2.run(story, ctx, FakeLLM(data), "hash")


def test_severity_clamped_and_frame_evidence_kept(ctx, tmp_path):
    result = _run_with_image(ctx, tmp_path, {
        "findings": [{
            "check_id": "T2.visual_text", "severity": "P3",  # judge lowballs
            "confidence": 0.93, "summary": "burned-in typo",
            "json_path": "pages[0].asset_url", "frame_ts": 2.0,
            "evidence_excerpt": "SEALS UNTIED"}],
        "abstain": False})
    f = result.findings[0]
    assert f.severity == "P1"          # catalog clamp beats the judge's opinion
    assert f.evidence.frame_ts == 2.0
    assert f.tier == 2


def test_unknown_t2_check_ids_dropped_and_abstain_p3(ctx, tmp_path):
    result = _run_with_image(ctx, tmp_path, {
        "findings": [{
            "check_id": "T2.looks_fine_approved", "severity": "P3",
            "confidence": 1.0, "summary": "ok", "json_path": "pages[0].asset_url",
            "evidence_excerpt": "x"}],
        "abstain": True, "abstain_reason": "image too degraded"})
    assert [f.check_id for f in result.findings] == ["T2.abstain"]
    assert result.findings[0].severity == "P3"
    assert result.abstained
