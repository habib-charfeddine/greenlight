"""Replay cache behavior — the offline backbone. No network, ever."""
import json

from greenlight.llm import LLMClient, ReplayCache, replay_key


def _seed(path, key, response, source="hand_authored"):
    entry = {"key": key, "model": "m", "prompt_version": "v", "content_hash": "h",
             "story_id": "s", "source": source, "response": response,
             "cost_usd": 0.0123, "latency_ms": 45, "recorded_at": "2026-08-14T00:00:00Z"}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def test_replay_cache_roundtrip_and_last_write_wins(tmp_path):
    p = tmp_path / "replay.jsonl"
    _seed(p, "k1", {"findings": []})
    _seed(p, "k1", {"findings": [{"x": 1}]})
    cache = ReplayCache(p)
    assert cache.get("k1")["response"] == {"findings": [{"x": 1}]}
    assert cache.get("missing") is None


def test_judge_replay_hit_returns_recorded_data(settings, tmp_path):
    p = tmp_path / "replay.jsonl"
    cfg = settings["models"]["tier1"]
    key = replay_key(cfg["model"], cfg["prompt_version"], "pol-1", "hash123")
    _seed(p, key, {"findings": [], "abstain": False, "headline": ""})
    llm = LLMClient(settings, mode="replay", replay_path=p)
    resp = llm.judge(tier="tier1", system_text="sys", user_content="u",
                     schema={}, content_hash="hash123", policy_version="pol-1",
                     story_id="s")
    assert resp.data == {"findings": [], "abstain": False, "headline": ""}
    assert resp.cost_usd == 0.0123
    assert llm.stats["replay_hits"] == 1


def test_policy_version_is_part_of_the_key(settings, tmp_path):
    """Judgments recorded under one tenant policy must not replay under
    another — the policy text is baked into the judge's system prompt."""
    p = tmp_path / "replay.jsonl"
    cfg = settings["models"]["tier1"]
    key = replay_key(cfg["model"], cfg["prompt_version"], "pol-OLD", "hash123")
    _seed(p, key, {"findings": [], "abstain": False, "headline": ""})
    llm = LLMClient(settings, mode="replay", replay_path=p)
    resp = llm.judge(tier="tier1", system_text="sys", user_content="u",
                     schema={}, content_hash="hash123",
                     policy_version="pol-NEW", story_id="s")
    assert resp.source == "replay_miss"


def test_live_mode_never_trusts_hand_authored_entries(settings, tmp_path, monkeypatch):
    """A hand-authored seed must not shadow a real API judgment in --live."""
    p = tmp_path / "replay.jsonl"
    cfg = settings["models"]["tier1"]
    key = replay_key(cfg["model"], cfg["prompt_version"], "pol-1", "hash123")
    _seed(p, key, {"findings": [], "abstain": False, "headline": ""},
          source="hand_authored")
    llm = LLMClient(settings, mode="live", replay_path=p)
    called = {}
    monkeypatch.setattr(llm, "_live_call",
                        lambda *a, **k: called.setdefault("live", True))
    llm.judge(tier="tier1", system_text="sys", user_content="u",
              schema={}, content_hash="hash123", policy_version="pol-1",
              story_id="s")
    assert called.get("live")  # went to the API, not the seed
    # ...but a recorded live entry IS reused (idempotent re-runs stay free)
    _seed(p, key, {"findings": [], "abstain": False, "headline": ""}, source="live")
    llm2 = LLMClient(settings, mode="live", replay_path=p)
    resp = llm2.judge(tier="tier1", system_text="sys", user_content="u",
                      schema={}, content_hash="hash123", policy_version="pol-1",
                      story_id="s")
    assert resp.source == "live"


def test_judge_replay_miss_degrades_without_network(settings, tmp_path):
    llm = LLMClient(settings, mode="replay", replay_path=tmp_path / "empty.jsonl")
    resp = llm.judge(tier="tier1", system_text="sys", user_content="u",
                     schema={}, content_hash="nope", policy_version="p",
                     story_id="s")
    assert resp.data is None
    assert resp.source == "replay_miss"
    assert llm.stats["replay_misses"] == 1
    assert llm._client is None  # the Anthropic client was never even constructed


def test_pricing_math(settings):
    llm = LLMClient(settings, mode="replay", replay_path="fixtures/ai_replay.jsonl")
    model = settings["models"]["tier1"]["model"]
    prices = settings["prices_usd_per_mtok"][model]
    cost = llm._price(model, {"input_tokens": 1000, "output_tokens": 200,
                              "cache_read_input_tokens": 0})
    expected = 1000 / 1e6 * prices["input"] + 200 / 1e6 * prices["output"]
    assert abs(cost - expected) < 1e-9
