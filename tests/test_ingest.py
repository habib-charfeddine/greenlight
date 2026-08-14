"""Feed loading: single object, list, malformed input."""
import json

import pytest

from greenlight.ingest import FeedError, ensure_story_id, load_feeds
from greenlight.models import Story


def test_single_feed_object(tmp_path, brief_feed):
    p = tmp_path / "feed.json"
    p.write_text(json.dumps(brief_feed.model_dump(mode="json")), encoding="utf-8")
    feeds = load_feeds(p)
    assert len(feeds) == 1
    assert feeds[0].stories[0].story_id == "story_123"


def test_list_of_feeds(tmp_path, brief_feed):
    p = tmp_path / "feeds.json"
    p.write_text(json.dumps([brief_feed.model_dump(mode="json")] * 2), encoding="utf-8")
    assert len(load_feeds(p)) == 2


def test_malformed_json_raises_feed_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(FeedError):
        load_feeds(p)


def test_missing_file_raises_feed_error(tmp_path):
    with pytest.raises(FeedError):
        load_feeds(tmp_path / "nope.json")


def test_story_without_id_gets_stable_synthetic_id():
    s = Story.model_validate({"story_title": "T", "pages": []})
    a = ensure_story_id(s, 0)
    b = ensure_story_id(s, 5)
    assert a.story_id.startswith("unknown-")
    assert a.story_id == b.story_id  # derived from content, not position
