"""Shared fixtures. Tests NEVER touch the network or the live API:
engines run in replay mode, HTTP goes through respx mocks or localhost only.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from greenlight.policy import EffectivePolicy, TenantPolicy, load_settings

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def settings() -> dict:
    return load_settings(REPO / "config" / "settings.yaml")


@pytest.fixture()
def afl_policy(settings) -> EffectivePolicy:
    import yaml
    with open(REPO / "config" / "tenants" / "antarctic_football_league.yaml",
              encoding="utf-8") as f:
        tenant = TenantPolicy.model_validate(yaml.safe_load(f))
    return EffectivePolicy(settings, tenant)


@pytest.fixture()
def ctx(afl_policy, settings, tmp_path):
    from greenlight.checks.base import CheckContext
    return CheckContext(policy=afl_policy, settings=settings,
                        http=httpx.Client(), store=None, offline=True,
                        tmpdir=tmp_path)


@pytest.fixture()
def brief_feed():
    from greenlight.models import Feed
    raw = json.loads((REPO / "fixtures" / "example_from_brief.json")
                     .read_text(encoding="utf-8"))
    return Feed.model_validate(raw)


def make_story(**overrides) -> "object":
    """A minimal clean story; override fields per test."""
    from greenlight.models import Story
    base = {
        "story_id": "story_t1",
        "story_title": "Match report: Penguin FC 2-1 Seals United",
        "pages": [{
            "page_id": "page_1", "type": "image",
            "asset_url": "http://localhost:8100/assets/story_t1/page_1.jpg",
            "action": {"cta": "Read match report",
                       "url": "http://localhost:8100/site/match-report"},
        }],
        "context": {"categories": ["Penguin FC", "Seals United"],
                    "tenant": "Antarctic Football League",
                    "publish_date": "2026-08-01"},
    }
    base.update(overrides)
    return Story.model_validate(base)
