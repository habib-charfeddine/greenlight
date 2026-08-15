# Greenlight eval report

- seed: 1337
- stories: 40 labeled, 40 verdicts
- tenants: 2, defect rate: 0.35
- date: 2026-08-15 (UTC)
- mode: live (Anthropic API, responses recorded to the replay cache)

## Per-check scores

| check_id | seeded | caught | recall | precision | strict-path | FP (clean) |
| --- | --- | --- | --- | --- | --- | --- |
| T0.asset_reachable | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.asset_type_match | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.cta_domain_policy | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.cta_link_health | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.date_logic | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.dup_asset | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.dup_story | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.image_blur | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.image_decodes | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.image_resolution | 2 | 2 | 100% | 100% | 2 | 0 |
| T0.image_solid | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.pii_leak | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.placeholder_text | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.profanity_lexicon | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.schema_valid | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.text_style_caps | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.text_style_emoji | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.video_black | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.video_probe | 1 | 1 | 100% | 100% | 1 | 0 |
| T0.video_silent | 1 | 1 | 100% | 100% | 1 | 0 |
| T1.coherence | 1 | 0 | 0% | - | 0 | 0 |
| T1.cta_copy_mismatch | 1 | 1 | 100% | 100% | 0 | 0 |
| T1.injection_attempt | 1 | 1 | 100% | 100% | 1 | 0 |
| T1.safety_screen | 1 | 1 | 100% | 100% | 1 | 0 |
| T1.spelling_grammar | 1 | 1 | 100% | 100% | 1 | 0 |
| T1.tone_style | 2 | 2 | 100% | 100% | 2 | 0 |
| T2.thumb_title_match | 1 | 1 | 100% | 20% | 1 | 4 |
| T2.visual_brand | 0 | 0 | - | 0% | 0 | 4 |
| T2.visual_quality | 0 | 0 | - | 0% | 0 | 2 |
| T2.visual_text | 1 | 1 | 100% | 100% | 1 | 0 |

## Overall

| metric | value |
| --- | --- |
| P0/P1 catch rate (seeded) | 96% (22/23) |
| Flag rate on clean stories | 17% (4/24) |
| Human review load (AMBER+RED, all stories) | 48% |
| Median latency / story | 15274 ms |
| Mean cost / story | $0.0076 |
| Cost per 1k stories | $7.59 |

## Scoring rules

A seeded defect counts as caught when a finding with the expected check_id lands on the labeled story; strict-path additionally requires a finding whose evidence.json_path equals a label json_path for that check on that story. Precision counts false positives ONLY on clean stories (empty label list): FP = clean stories with at least one finding for the check, precision = caught / (caught + FP). Findings on seeded stories beyond their labels are not counted as false positives — a seeded defect often legitimately trips neighbouring checks (a dead typosquat domain also fails link health), so cross-check contamination on seeded stories is excluded by design. P3 findings (which include every UNVERIFIABLE info finding) never count as false positives: they do not alert.

## No eval coverage

Catalog checks never exercised by a seeded defect — recall is unknown for these, not perfect:

- `T0.missing_action`
- `T1.factual_smell`
- `T2.visual_brand`
- `T2.visual_quality`
- `T2.visual_safety`

## Replay provenance

Replay cache ai_replay.jsonl: 183 entries (122 hand_authored, 61 live). Hand-authored entries demonstrate pipeline plumbing, not model skill — live-recorded numbers replace them after a --live run.
