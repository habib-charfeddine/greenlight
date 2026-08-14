"""Tier-0 text/schema checks against crafted stories — pure code, no HTTP."""
from greenlight.checks import tier0_schema

from .conftest import make_story


def _ids(findings):
    return [f.check_id for f in findings]


def test_placeholder_text_word_bounded(ctx):
    hit = tier0_schema.run(make_story(story_title="Ticket info TBD for the derby"), ctx)
    assert "T0.placeholder_text" in _ids(hit)
    # "drafted" contains "draft" but is a real word — must NOT fire
    clean = tier0_schema.run(make_story(story_title="Penguin FC drafted a new keeper"), ctx)
    assert "T0.placeholder_text" not in _ids(clean)


def test_pii_email_and_phone_but_not_scorelines(ctx):
    email = tier0_schema.run(make_story(story_title="Questions? email jo@club.com"), ctx)
    assert "T0.pii_leak" in _ids(email)
    phone = tier0_schema.run(make_story(story_title="Call +44 20 7946 0958 for tickets"), ctx)
    assert "T0.pii_leak" in _ids(phone)
    score = tier0_schema.run(make_story(story_title="Penguin FC win 3-2 in the derby"), ctx)
    assert "T0.pii_leak" not in _ids(score)
    season = tier0_schema.run(make_story(story_title="Season review 2025-2026 highlights"), ctx)
    assert "T0.pii_leak" not in _ids(season)


def test_profanity_word_bounded_scunthorpe_guard(ctx):
    hit = tier0_schema.run(make_story(story_title="What a shit result for the visitors"), ctx)
    assert "T0.profanity_lexicon" in _ids(hit)
    scunthorpe = tier0_schema.run(make_story(story_title="Scunthorpe away next Saturday"), ctx)
    assert "T0.profanity_lexicon" not in _ids(scunthorpe)


def test_caps_ratio_with_length_floor(ctx):
    shouty = tier0_schema.run(make_story(story_title="PENGUIN FC DESTROY SEALS UNITED"), ctx)
    assert "T0.text_style_caps" in _ids(shouty)
    acronym = tier0_schema.run(make_story(story_title="PFC V SU"), ctx)  # under floor
    assert "T0.text_style_caps" not in _ids(acronym)


def test_emoji_budget_is_tenant_policy(ctx):
    over = tier0_schema.run(make_story(story_title="Derby day! 🐧🐧🔥🔥⚽⚽"), ctx)
    assert "T0.text_style_emoji" in _ids(over)          # AFL allows 2
    under = tier0_schema.run(make_story(story_title="Derby day! 🐧⚽"), ctx)
    assert "T0.text_style_emoji" not in _ids(under)


def test_date_logic_stale_matchday(ctx):
    stale = make_story(
        story_title="Matchday build-up: PFC v SU",
        context={"categories": ["Penguin FC", "Seals United", "PFC v SU – 14/02/26"],
                 "tenant": "AFL", "publish_date": "2026-02-16"})
    assert "T0.date_logic" in _ids(tier0_schema.run(stale, ctx))
    on_time = make_story(
        story_title="Matchday build-up: PFC v SU",
        context={"categories": ["PFC v SU – 14/02/26"], "tenant": "AFL",
                 "publish_date": "2026-02-14"})
    assert "T0.date_logic" not in _ids(tier0_schema.run(on_time, ctx))
    # post-event language published later is fine — only pre-event titles go stale
    report = make_story(
        story_title="Match report: PFC v SU",
        context={"categories": ["PFC v SU – 14/02/26"], "tenant": "AFL",
                 "publish_date": "2026-02-16"})
    assert "T0.date_logic" not in _ids(tier0_schema.run(report, ctx))


def test_schema_valid_severities(ctx):
    from greenlight.models import Story
    missing_ctx = Story.model_validate({"story_id": "s", "story_title": "T", "pages": [
        {"page_id": "p", "type": "image", "asset_url": "http://localhost/x.jpg"}]})
    sevs = {(f.evidence.json_path, f.severity)
            for f in tier0_schema.run(missing_ctx, ctx)
            if f.check_id == "T0.schema_valid"}
    assert ("context", "P1") in sevs
    unknown_type = make_story(pages=[{"page_id": "p", "type": "carousel",
                                      "asset_url": "http://localhost/x.jpg"}])
    findings = tier0_schema.run(unknown_type, ctx)
    assert any("unknown type" in f.summary and f.severity == "P3" for f in findings)
