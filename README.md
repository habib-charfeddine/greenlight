# Greenlight — a publish-confidence layer for high-volume story feeds

Every published story becomes an evidence-backed **GREEN / AMBER / RED** verdict,
automatically and repeatedly. Humans only look where attention is earned.

![demo](docs/demo.gif) *(demo GIF placeholder — `make demo`, then: triage queue →
story_123 case file → /metrics)*

## The real issue

At high publishing volume, trust becomes an unobserved property: nobody can
answer *"is everything we just shipped correct, on-brand, and working?"* per
item. Errors get discovered by fans, sponsors, or clients — after publish.
Manual QA doesn't scale, sampling misses tail risk, and most publish-confidence
failures are boring and mechanical: dead links, broken media, placeholder text,
stale matchday posts. Those don't need AI — they need deterministic code that
runs on everything. AI earns its cost only on judgment calls (typos, tone,
coherence, what's actually in the pixels).

**The brief's own sample data demonstrates the class.** In `story_123`, page_2's
CTA says **"Buy tickets"** but links to **`/highlights`**, and page_1 says
**"Watch highlights"** but links to **`/match-report`**. Greenlight flags both,
offline, with JSON paths and a suggested fix:

```
P1 T1.cta_copy_mismatch  CTA 'Watch highlights' links to /match-report   pages[0].action.url
P1 T1.cta_copy_mismatch  CTA 'Buy tickets' links to /highlights          pages[1].action.url
```

(If the swap is intentional, a reviewer dismisses it once — the feedback loop
is how per-tenant thresholds tune over time.)

## What I built

```mermaid
%% see docs/architecture.mmd
```

A 60-second tour: a **feed poller** (cursor + content hash — unchanged stories
are never re-judged) feeds a **tiered cascade**: Tier 0 deterministic checks
(links, media, dates, dupes, placeholders — free, confidence 1.0, run on
everything) → Tier 1 text judge (Haiku-class, one structured-output call per
story) → Tier 2 vision judge (Sonnet-class, only on escalation / 15% sample /
tenant burn-in) → **deterministic verdict synthesis** (severity × confidence,
precision-first) → routing (RED → Slack-shaped webhook now; AMBER → review
queue; P2 → digest; GREEN → stored) → a **triage dashboard** with per-finding
evidence and Confirm/Dismiss feedback → a **self-scoring eval** that seeds
known defects and measures precision/recall/cost on every run.

## Quickstart

```bash
git clone <repo> && cd greenlight
uv sync --group dev          # or: pip install -e ".[dev]" with Python 3.11+
make demo                    # Windows: uv run greenlight demo
# → dashboard on http://localhost:8000, mock feed on :8100 — no API key needed
```

Replay mode (the default) runs fully offline from `fixtures/ai_replay.jsonl`.
For live judging: `export ANTHROPIC_API_KEY=... && uv run greenlight ingest
--feed http://localhost:8100/feed.json --live` (records real responses into the
replay cache).

`make eval` · `make test` · `make watch` — or the same `uv run greenlight ...`
subcommands on Windows.

## Results (seeded-defect eval, replay mode)

<!-- EVAL_TABLE -->
*(populated by `make eval` — see eval/report.md)*

**Honesty notes.**
- Replay-mode Tier-1/2 rows are backed by **hand-authored** replay fixtures
  derived from the golden labels (`scripts/seed_replay.py`, entries marked
  `"source": "hand_authored"`). They demonstrate pipeline plumbing —
  escalation, clamping, synthesis, routing — **not model skill**. A `--live`
  run replaces them with recorded model outputs and real cost/latency numbers.
- Synthetic seeds ≠ production distribution. The next validation step is a
  shadow run on a real tenant feed, measuring agreement with the humans who do
  this triage today.

## Cost

| Tier | Model | When it runs | List price basis | Est. cost/story |
|---|---|---|---|---|
| 0 | pure Python/ffmpeg | always | — | ~$0 |
| 1 | `claude-haiku-4-5` | every new/changed story | $1 / $5 per MTok | ~$0.002 |
| 2 | `claude-sonnet-5` | ~15–25% (escalation+sample) | $3 / $15 per MTok | ~$0.014/escalated |

Blended: **roughly $3–6 per 1,000 stories** at list prices; Batch API halves AI
cost for backfills. Model IDs and prices live in `config/settings.yaml`, not in
code; every finding carries its measured cost. *(Caveat verified at build time:
prompt-cache reads don't apply at our prompt sizes — the minimum cacheable
prefix is 4096 tokens on Haiku 4.5 — so cost math assumes no cache discount.
Sonnet 5 intro pricing $2/$10 through 2026-08-31 would land below these
numbers.)*

## Decisions & assumptions

- **Advisory, never blocking** — Storyteller's CMS ships Draft→Published with no
  approval step; a monitor that fails can't break anyone's matchday.
  `gate_mode: hold_red` exists in config and raises `NotImplementedError` with
  a roadmap note — blocking is a flag away once precision is proven.
- **Deterministic before AI** — most failures are mechanical; code catches them
  for free at confidence 1.0.
- **Precision-first routing** — severity × confidence; low-confidence AI
  findings downgrade one level; UNVERIFIABLE (bot-blocked/infra) never alerts.
- **Policy is per-tenant YAML, not code** — locale, banned terms, domain
  allowlist, thresholds, kill switches, severity overrides.
- **Untrusted content, injection-defended** — story text is data; embedded
  instructions are themselves a defect (`T1.injection_attempt`); the output
  schema's check_id enum + server-side severity clamp mean injected text can't
  invent an "approved" state. Proven by seeded eval + tests.
- **Idempotent re-runs** — verdict cache keyed (content_hash, policy_version,
  prompt_version); re-running is free.
- **Costs metered per finding** — from the API usage block × configured prices.

Assumptions: the schema extends the brief's snippet without renaming anything;
the feed is poll-able JSON; assets are fetchable in live mode (offline mode
reports external links UNVERIFIABLE rather than guessing); en locale.

## How this shows up in the product over time

Now: sidecar + triage dashboard + RED webhook + per-tenant health. Next: a
"Confidence" column and finding chips inside the CMS story list; one-click
"apply suggested fix"; reviewer feedback auto-tuning per-tenant thresholds
(dismiss twice → auto-quiet the pattern). Later: pre-publish gate mode per
tenant; live-event priority lane; content-quality SLA for CSMs ("99% of
stories defect-free, median time-to-detect < 2 min").

## With engineering support, first

1. Real CMS/API integration + webhook ingestion instead of polling.
2. Reviewer feedback wired to per-tenant threshold tuning.
3. CMS UI embedding (confidence column + chips).
4. Live-event priority lane with latency SLOs.
5. Batch-API backfill over a tenant's whole catalog ("content debt" report).

## Deliberately not built yet

Auto-write-back fixes (trust first); blocking gates by default (product
promise); ASR/transcript checks on video audio (roadmap: speech profanity);
embedding-based style memory (needs data volume); i18n beyond en-GB/en-US;
reviewer workflow management (assignments/SLAs); fine-tuning (golden set far
too small — prompts + thresholds + feedback loop is the right first rung).

## How AI was used

See [docs/AI_USAGE.md](docs/AI_USAGE.md). In three bullets: (1) this repo was
built by driving Claude Code against a pre-written spec kit (strategy, check
catalog, prompts, eval design) — the agent's key outputs were verified at
milestone checkpoints, and three of the kit's API assumptions were corrected
against current docs at build time; (2) at runtime, two versioned judge prompts
with enforced JSON schemas power Tiers 1–2, with replay caching making every
demo and test offline-deterministic; (3) eval numbers are never invented — the
table above is pasted from a real `make eval` run, and hand-authored replay
fixtures are explicitly marked as such.
