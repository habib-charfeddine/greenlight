# How AI built — and powers — Greenlight

Two halves, per the brief's ask ("how you used AI and how you verified outputs"):
**AI at build time** (this repo was built by driving Claude Code against a
pre-written spec kit) and **AI at runtime** (the Tier 1/2 judges inside the product).

## AI at build time

### Division of labor — what I did, what the agent did

I treated this the way I'd run any AI-leveraged project: **the thinking happened
before the agent was ever prompted**, and the agent multiplied my two hands-on
hours into a full build. Concretely:

**Mine (designed before and directed during the build):**

- **The problem framing.** Deciding this is *not* a content-moderation task but
  a publish-confidence problem — most failures are boring and mechanical, so
  deterministic code runs on everything and AI is spent only on judgment calls.
  Every product bet in the README (advisory-not-blocking, precision-first,
  policy-as-config, evidence-or-it-didn't-happen) was made by me, in writing,
  before a line of code existed.
- **Spotting the planted defect.** While reading the brief I noticed its own
  sample data has "Buy tickets" linking to `/highlights` and "Watch highlights"
  linking to `/match-report`. I made catching that the build's central
  acceptance test — it's the first thing the demo shows.
- **The full specification the agent built against:** the check catalog (every
  check_id, threshold, and severity as stable API), both judge prompts with
  their JSON schemas and the layered injection defense, the seeded-defect eval
  design (self-scoring was my requirement, not an afterthought), and the
  dashboard spec down to its design tokens.
- **The guardrails.** The agent operated under rules I wrote: work fully
  offline by default; never invent an eval number — paste real output only;
  mark every hand-authored replay entry as such; verify model IDs, prices, and
  SDK APIs against current docs instead of trusting training memory; self-check
  the acceptance criteria at milestone boundaries; keep the code boring. Every
  honesty property a reviewer can check in this repo traces to one of those
  rules.
- **Milestone gates and final judgment.** I reviewed at checkpoints, decided
  what was submit-blocking versus roadmap, and made the scoping calls (what to
  cut, what to defer, what to state as a known gap).

**The agent's (Claude Code):** writing the code against the frozen contracts,
fanning independent modules out to parallel sub-agents, integrating, authoring
tests, running the eval/demo loops, and executing the adversarial review I
required before submission.

**The point of the design:** the corrections logged below were caught *by the
verification structure I set up* — not by luck. The build log is the evidence.

### Build log (what I directed, what the agent produced, what got caught)

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
- **M1–M4 parallel build.** Six sub-agents authored the independent modules
  (feedgen, three Tier-0 check groups, dashboard, eval harness) against the
  frozen M0 contracts while the orchestrating agent wrote the coupled core
  (LLM wrapper, judges, engine, CLI) inline. Integration fixes made by the
  orchestrator, not the module authors:
  - *Offline fetch policy* (design decision, not in the kit): in replay mode
    only localhost is fetched; external hosts return UNVERIFIABLE without
    network I/O. This is what keeps the brief fixture AMBER (its
    `cdn.storyteller.com` assets would otherwise be BROKEN→RED), keeps eval
    deterministic, and honors "tests never touch the network".
  - *Connection-refused vs DNS failure*: server-down is infrastructure →
    UNVERIFIABLE (never pages); NXDOMAIN is a dead domain in content → BROKEN.
  - *Headline persistence*: judge-written headlines now stored on the verdict
    row so the case file shows the model's sentence, not a derived fallback.
  - *Test-ordering bug caught in review*: replay caches load at engine
    construction; a test seeded entries after constructing the engine and
    silently exercised the miss path — restructured.
- **Verification at milestone boundaries**: brief fixture ingested end-to-end
  offline (story_123 AMBER + both CTA-mismatch findings with json_paths;
  re-run skips both stories via content-hash cache); all dashboard routes
  render against a real store; injection pair proven by test.
- **The precision pass (the eval doing its job).** First eval run: P0/P1 catch
  rate 100%, but `T0.dup_asset` precision 12% and `T0.dup_story` 20% — seven
  clean stories false-flagged, clean-story flag rate 17% (target ≤10%).
  Instead of guessing, we measured the eval world's pairwise pHash distances:
  the true seeded re-publish pair sits at Hamming 0; clean stories sharing the
  tenant's house template floor at 6–8 — the catalog's `near ≤ 8` default is
  wrong for template-driven sport content. Tightened `phash_near_hamming`
  8 → 5 in settings (with the measurement in the comment). Re-run: dup checks
  100%/100%, clean flag rate 0%, catch rate still 100%. *Human should verify:*
  this threshold was tuned on synthetic covers; re-measure on a real tenant's
  media before trusting it.
- **Spec discrepancy caught by an agent and fixed at integration**: the 05
  seed matrix's "silent video" seed generated no audio stream, but the
  catalog's `T0.video_silent` measures volumedetect on an *existing* stream
  (absence is `video_probe`'s no-audio P2 — a different defect). The asset now
  carries a digital-silence audio track, so the check fires as cataloged.
- **Bug found only by actually running `make demo`** (51 green tests did not
  catch it): the demo spawned its children with the global `--db` flag *after*
  the argparse subcommand, so all three child processes crash-looped with
  "unrecognized arguments" while the parent warn-spammed forever. Fixed the
  argument order and made the demo stop loudly when any child dies. Lesson
  recorded here deliberately: unit tests validated every component, and the
  one thing that broke was the glue that only an end-to-end run exercises.
- **Bug found only by a fresh-clone run on a cp1252 console**: the RED-alert
  print carried a literal 🔴, which raises UnicodeEncodeError on stock Windows
  consoles — crashing the pipeline on its first RED verdict. Console output is
  now encoding-safe; the Slack payload uses the ASCII `:red_circle:` shortcode.
- **Adversarial pre-submit review** (five skeptical finder agents, each finding
  re-verified by a refuting agent before any fix). Confirmed and fixed:
  1. *Clean GREEN stories were re-judged every poll* — the judged-marker
     derived its policy_version from `findings[0]`, which is empty for clean
     stories. Now passed explicitly; regression tests at store and engine level.
  2. *Replay cache ignored tenant policy* — the key was
     (model, prompt_version, content_hash) while the policy text is baked into
     the judge's system prompt; a policy bump would have replayed stale
     judgments, and live mode consulted the cache before checking mode, so
     hand-authored seeds could shadow real API calls. Key now includes
     policy_version; live mode only trusts `source: live` entries.
  3. *Tenant kill switches couldn't stop vision spend* — escalation was
     computed on raw findings before `apply_overrides`; a disabled check could
     still buy a Tier-2 call. Overrides now apply before the trigger.
  4. *Capped downloads misdiagnosed as corrupt* — a >25MiB asset was truncated
     at the fetch cap and would fail image decode as if the tenant's file were
     broken. Truncation is now tracked and reported as a skip, never a defect.
  5. *Live API outages permanently swallowed AI checks* — errors marked the
     story judged, so it was never retried. Degraded stories now keep their
     Tier-0 verdict but stay queued for AI retry next cycle (05 edge-case list).
  6. *One sloppy judge finding sank its valid siblings* — whole-response
     validation dropped a valid P0 when a sibling had an over-long string.
     Findings are now validated individually (strings clipped, confidence
     clamped, hopeless ones dropped and counted).
  Plus backlog items: `greenlight eval --live` wasn't wired through, the pip
  fallback install path couldn't see dev deps, the README overclaimed a digest
  view the spec's cut list defers, the offline demo world had no replay
  coverage (every story showed "judge unavailable"), and the eval's exact
  pHash numbers are font/platform-dependent — all fixed or reworded.
  Two review claims were refuted by the verifiers (they described pre-fix
  code) and correctly not "fixed" twice.
- **Live fire (I ran it, 2026-08-15).** 61 real API calls recorded into the
  replay cache ($0.30 total): Haiku 4.5 judged all 40 eval stories, Sonnet
  judged 21 escalations. Results kept exactly as measured, per the
  never-invent-numbers rule: P0/P1 catch rate 96% — the real judge missed the
  seeded `T1.coherence` defect the hand-authored fixture had assumed it would
  catch (tuning next: a worked example in that rubric line) — and the vision
  judge flagged 4 synthetic "clean" covers for violating the tenant's own
  visual rules, which our generator had never made compliant. We documented
  the judge as arguably *right* and the synthetic world as wrong, rather than
  quietly re-rolling or reseeding. Measured costs landed on the estimates:
  $0.0021/story Tier 1 (est. $0.002), $0.0105/escalation Tier 2 (est. $0.014).

### Key prompts used (excerpts)

1. **Kickoff** (the kit's `08_MASTER_PROMPT.md`): build per spec, offline-first
   replay mode, never invent eval numbers, mark hand-authored replay entries,
   self-check acceptance criteria after M2/M5.
2. **Module fan-out**: six parallel sub-agents, each given the frozen contract
   files, the exact spec sections, a "write only your files / report deviations
   instead of fixing contracts" rule, and a required integration-notes report.
   Those reports caught the silent-video spec discrepancy before integration.
3. **Precision pass** (the kit's follow-up #3, executed after the first eval):
   "show the lowest-precision checks, propose a threshold tweak, apply, re-run,
   show before/after" — executed with a measured pHash-distance distribution
   rather than guesswork; before/after in the README results section.
4. **Injection proof** (follow-up #4): test asserting the seeded injection is
   flagged and cannot improve a verdict, plus a schema-firewall test where a
   fake "approved" check_id is dropped by the enum.
5. **Pre-submit review** (follow-up #6): a second multi-agent workflow — five
   skeptical finders (correctness, spec compliance, fresh-clone
   reproducibility, honesty audit, runtime probing) whose findings were each
   adversarially verified by a refuting agent before any fix was applied.

### What a human should verify before submitting

- Model IDs and prices in `config/settings.yaml` against the console; then run
  the live-fire step (follow-up #2) to replace hand-authored replay entries.
- Record the demo GIF (`make demo` → queue → story_123 → /metrics) — the
  README placeholder is waiting for it.
- The email draft, timebox statement, and IBAN/BIC (07_SUBMISSION.md).

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
