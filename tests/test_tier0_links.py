"""Link-health + domain-policy checks. HTTP is mocked with respx — no sockets."""
import httpx
import pytest
import respx

from greenlight.checks import tier0_links

from .conftest import make_story


def _ids(findings):
    return [f.check_id for f in findings]


def _story_with_urls(asset="http://localhost:8100/assets/s/p.jpg",
                     cta="http://localhost:8100/site/tickets",
                     cta_text="Buy tickets"):
    return make_story(pages=[{"page_id": "p1", "type": "image", "asset_url": asset,
                              "action": {"cta": cta_text, "url": cta}}])


@respx.mock
def test_ok_links_produce_no_findings(ctx):
    respx.get("http://localhost:8100/assets/s/p.jpg").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/jpeg"},
                                    content=b"x" * 10))
    respx.get("http://localhost:8100/site/tickets").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"},
                                    content=b"<html/>"))
    findings = tier0_links.run(_story_with_urls(), ctx)
    assert findings == []


@respx.mock
def test_404_asset_is_p0_broken(ctx):
    respx.get("http://localhost:8100/assets/s/p.jpg").mock(
        return_value=httpx.Response(404))
    respx.get("http://localhost:8100/site/tickets").mock(
        return_value=httpx.Response(200, content=b"ok"))
    findings = tier0_links.run(_story_with_urls(), ctx)
    broken = [f for f in findings if f.check_id == "T0.asset_reachable"]
    assert broken and broken[0].severity == "P0"
    assert broken[0].evidence.http_status == 404


@respx.mock
def test_403_is_unverifiable_p3_never_alerts(ctx):
    respx.get("http://localhost:8100/assets/s/p.jpg").mock(
        return_value=httpx.Response(403))
    respx.get("http://localhost:8100/site/tickets").mock(
        return_value=httpx.Response(429))
    findings = tier0_links.run(_story_with_urls(), ctx)
    assert {f.severity for f in findings} == {"P3"}


@respx.mock
def test_content_type_mismatch(ctx):
    respx.get("http://localhost:8100/assets/s/p.jpg").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/jpeg"},
                                    content=b"x"))
    respx.get("http://localhost:8100/site/tickets").mock(
        return_value=httpx.Response(200, content=b"ok"))
    story = make_story(pages=[{"page_id": "p1", "type": "video",  # video page, jpeg body
                               "asset_url": "http://localhost:8100/assets/s/p.jpg",
                               "action": {"cta": "Buy tickets",
                                          "url": "http://localhost:8100/site/tickets"}}])
    findings = tier0_links.run(story, ctx)
    mismatch = [f for f in findings if f.check_id == "T0.asset_type_match"]
    assert mismatch and mismatch[0].severity == "P1"


def test_domain_policy_offline_no_sockets_needed(ctx):
    # External hosts never get fetched offline; the domain checks are pure string
    # logic and must still fire.
    story = make_story(pages=[
        {"page_id": "p1", "type": "image",
         "asset_url": "http://localhost:8100/assets/s/p1.jpg",
         "action": {"cta": "Buy tickets", "url": "https://bit.ly/x"}},
        {"page_id": "p2", "type": "image",
         "asset_url": "http://localhost:8100/assets/s/p2.jpg",
         "action": {"cta": "Shop now", "url": "https://antarcticfootbal1league.com/shop"}},
        {"page_id": "p3", "type": "image",
         "asset_url": "http://localhost:8100/assets/s/p3.jpg",
         "action": {"cta": "Vote now", "url": "http://203.0.113.9/poll"}},
    ])
    findings = [f for f in tier0_links.run(story, ctx)
                if f.check_id == "T0.cta_domain_policy"]
    by_path = {f.evidence.json_path: f for f in findings}
    assert by_path["pages[0].action.url"].severity == "P1"   # shortener
    assert by_path["pages[1].action.url"].severity == "P0"   # typosquat (lev 1)
    assert by_path["pages[2].action.url"].severity == "P1"   # raw IP


def test_allowed_domains_and_subdomains_pass(ctx):
    story = _story_with_urls(cta="https://tickets.antarcticfootballleague.com/derby")
    findings = [f for f in tier0_links.run(story, ctx)
                if f.check_id == "T0.cta_domain_policy"]
    assert findings == []
