"""Data-contract behavior: leniency + hash stability."""
import json

from greenlight.models import Feed, Story, content_hash

from .conftest import make_story


def test_content_hash_is_key_order_invariant():
    a = Story.model_validate(json.loads(
        '{"story_id": "s1", "story_title": "T", "pages": []}'))
    b = Story.model_validate(json.loads(
        '{"story_title": "T", "pages": [], "story_id": "s1"}'))
    assert content_hash(a) == content_hash(b)


def test_content_hash_changes_with_content():
    a = make_story()
    b = make_story(story_title="Different title")
    assert content_hash(a) != content_hash(b)


def test_unknown_extra_fields_are_kept_not_fatal():
    story = Story.model_validate({
        "story_id": "s1", "story_title": "T", "pages": [],
        "future_field": {"nested": True},
    })
    assert story.story_id == "s1"
    # extras participate in the hash: a schema extension = changed content
    assert content_hash(story) != content_hash(make_story(story_id="s1", story_title="T", pages=[]))


def test_missing_context_and_empty_stories_tolerated():
    feed = Feed.model_validate({"tenant_id": "t", "tenant_name": "T",
                                "last_synced_at": None, "stories": []})
    assert feed.stories == []
    story = Story.model_validate({"story_id": "s", "story_title": "x", "pages": [
        {"page_id": "p", "type": "image", "asset_url": "http://x/y.jpg"}]})
    assert story.context is None
    assert story.pages[0].action is None
