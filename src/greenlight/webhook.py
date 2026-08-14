"""RED alert routing: Slack-shaped JSON to a configured webhook URL + stdout.

Always prints to stdout (the demo proof); POSTs only when a URL is configured.
Delivery failure is logged, never fatal — alerting must not break the pipeline.
"""
from __future__ import annotations

import json

import httpx

from .models import StoryVerdict


def build_payload(verdict: StoryVerdict, story_title: str) -> dict:
    top = next((f for f in verdict.findings if f.severity == "P0"),
               verdict.findings[0] if verdict.findings else None)
    lines = [f"*{f.severity}* `{f.check_id}` — {f.summary}"
             for f in verdict.findings if f.severity in ("P0", "P1")][:5]
    return {
        "text": f"🔴 RED: “{story_title}” ({verdict.story_id}) — "
                f"{top.summary if top else 'see case file'}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"*RED verdict* — <http://localhost:8000/story/{verdict.story_id}"
                     f"|{story_title}>\nscore {verdict.score} · "
                     f"tenant `{verdict.tenant_id}`"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines) or "—"}},
        ],
    }


def send_red_alert(verdict: StoryVerdict, story_title: str, settings: dict) -> None:
    payload = build_payload(verdict, story_title)
    print(f"[RED ALERT] {json.dumps(payload['text'], ensure_ascii=False)}")
    url = (settings.get("webhook") or {}).get("url")
    if not url:
        return
    try:
        httpx.post(url, json=payload, timeout=5.0)
    except httpx.HTTPError as e:
        print(f"[webhook] delivery failed (non-fatal): {e}")
