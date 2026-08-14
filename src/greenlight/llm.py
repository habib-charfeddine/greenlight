"""Anthropic client wrapper: structured outputs, replay cache, cost metering.

Verified against SDK 0.122.0 at build time (2026-08-14):
- Structured outputs via ``output_config={"format": {"type": "json_schema", ...}}``
  (the canonical surface; the old ``output_format`` param is gone). Schemas must
  not contain ``minimum``/``maximum``/``maxLength`` — those are validated
  client-side by the judge modules' Pydantic models instead.
- Retries: the SDK retries 429/5xx with exponential backoff + jitter and honors
  ``retry-after``; we set ``max_retries=3`` per the build spec.
- Prompt caching: ``cache_control`` goes on the system/policy block. NOTE: the
  minimum cacheable prefix is 4096 tokens on Haiku 4.5 and 1024 on Sonnet 5 —
  our policy blocks are shorter, so this is a silent no-op today. We keep the
  marker (correct pattern; kicks in as policy packs grow) and price all calls
  WITHOUT a cache discount.

Replay cache (``fixtures/ai_replay.jsonl``): one JSON line per judge response,
keyed sha256(model + prompt_version + policy_version + content_hash) — the
policy_version is in the key because the tenant policy is rendered into the
judge's system prompt, so a policy change must invalidate cached judgments.
``source`` marks each entry ``hand_authored`` (seeded so offline demos show the
case) or ``live`` (recorded from a real API call). Replay mode never touches
the network and accepts any entry; live mode only trusts ``source: live``
entries — hand-authored seeds must never shadow a real API judgment.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .models import utcnow

Mode = Literal["replay", "live"]


@dataclass
class JudgeResponse:
    """One judge call's outcome, replayed or live."""

    data: dict | None            # parsed JSON matching the judge schema; None = unavailable
    model: str
    prompt_version: str
    cost_usd: float
    latency_ms: int
    source: str                  # "hand_authored" | "live" | "replay_miss" | "error"
    error: str | None = None


def replay_key(model: str, prompt_version: str, policy_version: str,
               content_hash: str) -> str:
    return hashlib.sha256(
        f"{model}|{prompt_version}|{policy_version}|{content_hash}".encode()
    ).hexdigest()


class ReplayCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._entries: dict[str, dict] = {}
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    self._entries[entry["key"]] = entry

    def get(self, key: str) -> dict | None:
        return self._entries.get(key)

    def put(self, entry: dict) -> None:
        self._entries[entry["key"]] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class LLMClient:
    """Judge-call runner. One instance per Engine."""

    def __init__(self, settings: dict, mode: Mode = "replay",
                 replay_path: str | Path | None = None):
        self.settings = settings
        self.mode = mode
        self.replay = ReplayCache(replay_path or settings.get("replay_cache",
                                                              "fixtures/ai_replay.jsonl"))
        self.stats = {"replay_hits": 0, "replay_misses": 0, "live_calls": 0}
        self._client = None  # lazy: replay mode must work without the SDK ever connecting

    # -- pricing ------------------------------------------------------------

    def _price(self, model: str, usage: dict) -> float:
        prices = self.settings.get("prices_usd_per_mtok", {}).get(model)
        if not prices:
            return 0.0
        cost = usage.get("input_tokens", 0) / 1e6 * prices.get("input", 0.0)
        cost += usage.get("output_tokens", 0) / 1e6 * prices.get("output", 0.0)
        cost += usage.get("cache_read_input_tokens", 0) / 1e6 * prices.get("cached_read", 0.0)
        return round(cost, 6)

    # -- the one public call ------------------------------------------------

    def judge(
        self,
        *,
        tier: Literal["tier1", "tier2"],
        system_text: str,
        user_content: list[dict] | str,
        schema: dict,
        content_hash: str,
        policy_version: str,
        story_id: str,
    ) -> JudgeResponse:
        cfg = self.settings["models"][tier]
        model = cfg["model"]
        prompt_version = cfg["prompt_version"]
        key = replay_key(model, prompt_version, policy_version, content_hash)

        cached = self.replay.get(key)
        # Live mode only trusts recorded API responses; a hand-authored seed
        # must never stand in for a real judgment when the API is available.
        if cached is not None and (self.mode == "replay"
                                   or cached.get("source") == "live"):
            self.stats["replay_hits"] += 1
            return JudgeResponse(
                data=cached.get("response"),
                model=model,
                prompt_version=prompt_version,
                cost_usd=float(cached.get("cost_usd", 0.0)),
                latency_ms=int(cached.get("latency_ms", 0)),
                source=cached.get("source", "hand_authored"),
            )

        if self.mode == "replay":
            self.stats["replay_misses"] += 1
            return JudgeResponse(
                data=None, model=model, prompt_version=prompt_version,
                cost_usd=0.0, latency_ms=0, source="replay_miss",
                error="no replay entry for this (model, prompt_version, content_hash)",
            )

        return self._live_call(tier, cfg, model, prompt_version, key,
                               system_text, user_content, schema,
                               content_hash, policy_version, story_id)

    # -- live path ----------------------------------------------------------

    def _live_call(self, tier: str, cfg: dict, model: str, prompt_version: str,
                   key: str, system_text: str, user_content: list[dict] | str,
                   schema: dict, content_hash: str, policy_version: str,
                   story_id: str) -> JudgeResponse:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic(max_retries=3)

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": int(cfg.get("max_tokens", 1000)),
            "system": [{
                "type": "text",
                "text": system_text,
                # No-op below the model's minimum cacheable prefix — see module docstring.
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": user_content}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        if cfg.get("temperature") is not None:
            kwargs["temperature"] = cfg["temperature"]
        if cfg.get("thinking") == "disabled":
            kwargs["thinking"] = {"type": "disabled"}
        if cfg.get("effort"):
            kwargs["output_config"]["effort"] = cfg["effort"]

        t0 = time.monotonic()
        try:
            resp = self._client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 — degrade to Tier 0, never crash the run
            return JudgeResponse(
                data=None, model=model, prompt_version=prompt_version,
                cost_usd=0.0, latency_ms=int((time.monotonic() - t0) * 1000),
                source="error", error=f"{type(e).__name__}: {e}",
            )
        latency_ms = int((time.monotonic() - t0) * 1000)
        self.stats["live_calls"] += 1

        if resp.stop_reason not in ("end_turn", "stop_sequence"):
            return JudgeResponse(
                data=None, model=model, prompt_version=prompt_version,
                cost_usd=self._price(model, resp.usage.model_dump()),
                latency_ms=latency_ms, source="error",
                error=f"stop_reason={resp.stop_reason}",
            )

        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return JudgeResponse(
                data=None, model=model, prompt_version=prompt_version,
                cost_usd=self._price(model, resp.usage.model_dump()),
                latency_ms=latency_ms, source="error",
                error=f"non-JSON judge output: {e}",
            )

        usage = resp.usage.model_dump()
        cost = self._price(model, usage)
        self.replay.put({
            "key": key,
            "model": model,
            "prompt_version": prompt_version,
            "policy_version": policy_version,
            "content_hash": content_hash,
            "story_id": story_id,
            "source": "live",
            "response": data,
            "usage": {k: usage.get(k, 0) for k in
                      ("input_tokens", "output_tokens", "cache_read_input_tokens",
                       "cache_creation_input_tokens")},
            "cost_usd": cost,
            "latency_ms": latency_ms,
            "recorded_at": utcnow().isoformat(),
        })
        return JudgeResponse(
            data=data, model=model, prompt_version=prompt_version,
            cost_usd=cost, latency_ms=latency_ms, source="live",
        )
