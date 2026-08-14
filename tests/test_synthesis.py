"""Verdict synthesis is deterministic code — pin its rules."""
from greenlight.checks.base import make_finding
from greenlight.engine import effective_severity, synthesize
from greenlight.models import Evidence, Finding


def _finding(severity: str, tier: int = 0, confidence: float = 1.0) -> Finding:
    return Finding(check_id="T0.test", severity=severity, confidence=confidence,
                   summary="s", evidence=Evidence(), tier=tier, policy_version="p")


def test_verdict_mapping(settings):
    assert synthesize([], settings) == ("GREEN", 100)
    assert synthesize([_finding("P0")], settings)[0] == "RED"
    assert synthesize([_finding("P1")], settings)[0] == "AMBER"
    assert synthesize([_finding("P2")], settings)[0] == "GREEN"   # digest, not queue
    assert synthesize([_finding("P3")], settings)[0] == "GREEN"


def test_low_confidence_ai_findings_downgrade_one_level(settings):
    below = settings["confidence_downgrade_below"]
    shaky_ai = _finding("P1", tier=1, confidence=below - 0.01)
    assert effective_severity(shaky_ai, below) == "P2"
    confident_ai = _finding("P1", tier=1, confidence=below)
    assert effective_severity(confident_ai, below) == "P1"
    # deterministic checks never downgrade, whatever their confidence field says
    det = _finding("P0", tier=0, confidence=0.1)
    assert effective_severity(det, below) == "P0"
    # a downgraded P1 no longer drives AMBER
    assert synthesize([shaky_ai], settings)[0] == "GREEN"


def test_score_weights(settings):
    w = settings["severity_weights"]
    verdict, score = synthesize([_finding("P0"), _finding("P1"), _finding("P2")], settings)
    assert verdict == "RED"
    assert score == 100 - w["P0"] - w["P1"] - w["P2"]
    _, floored = synthesize([_finding("P0")] * 5, settings)
    assert floored == 0
