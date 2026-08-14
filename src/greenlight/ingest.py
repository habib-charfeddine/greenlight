"""Feed loading + delta detection.

A feed source is a local path or an HTTP URL, containing either one feed object
(the brief's schema) or a list of them (feedgen serves both tenants as a list).
Malformed feeds raise ``FeedError`` — the caller alerts and keeps the last good
cursor (edge-case list in 05_DATA_AND_EVAL).
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
from pydantic import ValidationError

from .models import Feed, Story, content_hash


class FeedError(Exception):
    pass


def load_feeds(source: str | Path, http: httpx.Client | None = None) -> list[Feed]:
    text: str
    src = str(source)
    if src.startswith(("http://", "https://")):
        client = http or httpx.Client(timeout=10.0)
        try:
            r = client.get(src)
            r.raise_for_status()
            text = r.text
        except httpx.HTTPError as e:
            raise FeedError(f"feed fetch failed: {e}") from e
    else:
        p = Path(src)
        if not p.exists():
            raise FeedError(f"feed file not found: {p}")
        text = p.read_text(encoding="utf-8")

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise FeedError(f"malformed feed JSON: {e}") from e

    items = raw if isinstance(raw, list) else [raw]
    feeds: list[Feed] = []
    for item in items:
        try:
            feeds.append(Feed.model_validate(item))
        except ValidationError as e:
            raise FeedError(f"feed failed schema validation: {e}") from e
    return feeds


def ensure_story_id(story: Story, index: int) -> Story:
    """A story without an id still needs tracking — synthesize a stable one."""
    if story.story_id:
        return story
    return story.model_copy(update={"story_id": f"unknown-{content_hash(story)[:8]}"})
