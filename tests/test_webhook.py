"""RED alerting must survive any console encoding and any story title."""
import io

from greenlight.models import Evidence, Finding, StoryVerdict, utcnow
from greenlight.webhook import _console_safe, build_payload, send_red_alert


def _red_verdict(title_irrelevant=None) -> StoryVerdict:
    return StoryVerdict(
        story_id="s1", tenant_id="t1", content_hash="h" * 64, verdict="RED",
        score=40, findings=[Finding(
            check_id="T0.cta_link_health", severity="P0", confidence=1.0,
            summary="CTA link is broken: HTTP 404", evidence=Evidence(),
            tier=0, policy_version="p")],
        checked_at=utcnow(), pipeline_version="0.1.0", totals={})


def test_payload_is_slack_shaped_and_ascii_shortcode():
    payload = build_payload(_red_verdict(), "Derby day 🐧🔥")
    assert payload["text"].startswith(":red_circle: RED:")
    assert "blocks" in payload and len(payload["blocks"]) == 2


def test_console_safe_survives_cp1252(monkeypatch):
    """A stock Windows console (cp1252) cannot encode emoji — the alert line
    must degrade to replacement chars, never raise (this crashed the pipeline
    on a fresh clone until fixed)."""
    class Cp1252Out(io.StringIO):
        encoding = "cp1252"
    fake = Cp1252Out()
    monkeypatch.setattr("sys.stdout", fake)
    send_red_alert(_red_verdict(), "Emoji title 🐧🔥 crashes cp1252", settings={})
    out = fake.getvalue()
    assert "[RED ALERT]" in out
    assert "\U0001f427" not in out  # replaced, not passed through


def test_console_safe_passes_utf8_through():
    assert "🐧" in _console_safe("🐧")  # utf-8-capable consoles keep the emoji
