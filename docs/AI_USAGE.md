# How AI built — and powers — Greenlight

Two halves, per the brief's ask ("how you used AI and how you verified outputs"):
**AI at build time** (this repo was built by driving Claude Code against a
pre-written spec kit) and **AI at runtime** (the Tier 1/2 judges inside the product).

## AI at build time

### Setup

The build was driven by Claude Code from a spec kit written before the session:
strategy, build spec, check catalog with stable IDs and thresholds, judge prompts
with JSON schemas, data/eval design, and dashboard spec. Claude Code executed
milestones M0–M5 against those specs; the human's job was direction, spot-checks
at milestone boundaries, and honesty control (no invented numbers).

### Build log (running — what the agent did, what a human should verify)

- **M0 scaffold.** Repo, pyproject (uv, pinned via `uv.lock`), data contracts
  (`models.py` mirrors the brief fixture exactly), tenant policy YAMLs, global
  settings, shared check protocol with 3-state HTTP logic.
- **Verified against current API docs instead of trusting the kit/memory** —
  three kit assumptions were corrected at build time:
  1. The kit's Tier-2 prompt spec says `temperature 0.2`; the current Sonnet
     (`claude-sonnet-5`) **rejects non-default sampling params** (400). We omit
     temperature on Tier 2 and disable its default-on adaptive thinking instead
     (a judge needs neither).
  2. The kit assumed the ~800-token cached policy block would earn 0.1× cache
     reads. Current minimum cacheable prefix is **4096 tokens on Haiku 4.5** /
     1024 on Sonnet 5 — below that, `cache_control` is a silent no-op. We still
     set it (correct pattern, kicks in as policy packs grow) but **cost math
     assumes no cache discount**.
  3. Structured outputs: the installed SDK (0.122.0) has `messages.parse` /
     `output_config.format` (the old `output_format` param is gone). Raw JSON
     schemas must drop `minimum/maximum/maxLength` (unsupported server-side);
     we validate those client-side with Pydantic instead.
- *Human should verify:* model IDs/prices in `config/settings.yaml` against
  the console before a live run; the profanity wordlist (small and en-only by
  design); thresholds in settings vs. the catalog.

### Key prompts used (excerpts)

1. The kickoff prompt (from the kit's `08_MASTER_PROMPT.md`): build per spec,
   offline-first replay mode, never invent eval numbers, mark hand-authored
   replay entries, self-check acceptance criteria after M2/M5.
   *(Further prompts appended as used.)*

## AI at runtime

- **Tiered cascade:** Tier 0 deterministic checks (free, confidence 1.0, always
  run) → Tier 1 text judge (Haiku 4.5, one structured-output call per story)
  → Tier 2 vision judge (Sonnet 5, escalation/sample only) → Tier 3 human.
- **Judge prompts** live in `04_prompts/` form, versioned (`t1-v1.0`,
  `t2-v1.0`); prompt_version is stamped on findings and keys the replay cache.
- **Structured output**: `output_config.format` JSON schema with `check_id`
  enum — the injection firewall's second layer (no "APPROVED" field exists to
  inject into). Engine clamps severities server-side per check_id.
- **Injection defense**: story text is wrapped in `<story_content>` tags and
  treated as data; embedded instructions are themselves a defect
  (`T1.injection_attempt`); the eval seeds one and asserts the verdict is
  unchanged.
- **Replay cache** (`fixtures/ai_replay.jsonl`): every judge response keyed by
  sha256(model + prompt_version + content_hash). Tests and `make demo` run
  fully offline. Each entry carries `source: hand_authored | live` — see
  README "Honesty notes".
- **Cost metering**: every AI finding carries its cost computed from the usage
  block × prices in `config/settings.yaml` (never hardcoded).
