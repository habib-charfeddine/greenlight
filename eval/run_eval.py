"""Self-scoring eval harness (M5, 05_DATA_AND_EVAL.md "Eval harness").

Generates a seeded feed world with feedgen (fixed seed), serves it locally,
runs the full Greenlight pipeline over it in replay mode against a throwaway
store, then scores the produced verdicts against feedgen's golden labels.
Output: aligned stdout tables plus a markdown report (eval/report.md), both
computed entirely from this run — no number is ever fabricated or cached.

Scoring rules (also printed in the report):

* recall: a seeded defect is CAUGHT when a finding with the expected check_id
  lands on the labeled story. strict-path additionally requires a finding
  whose evidence.json_path equals a label json_path for that check/story.
* precision: false positives are counted ONLY on clean stories (empty label
  list): FP_C = clean stories with >= 1 finding for check C, and
  precision = caught / (caught + FP_C). Findings on seeded stories beyond
  their labels are NOT counted as FPs — a seeded defect often legitimately
  trips neighbouring checks (a dead typosquat domain also fails link health),
  so cross-check contamination on seeded stories is excluded by design.
  P3 findings (which include every UNVERIFIABLE info finding) never count as
  FPs either: they do not alert.

This is a reporting tool, not a gate: the process always exits 0 so CI can
run it without the numbers failing the build.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: feedgen/ lives at the repo root and is not installed in the venv (only
#: src/greenlight is packaged) — make it importable regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from greenlight.checks.base import DEFAULT_SEVERITY  # noqa: E402
from greenlight.models import StoryVerdict  # noqa: E402
from greenlight.policy import load_settings, load_tenants  # noqa: E402
from greenlight.store import Store  # noqa: E402

#: World-generation knobs fixed by 05_DATA_AND_EVAL.md (the eval world is a
#: standard shape; only seed/story-count/port are operator-tunable).
EVAL_TENANTS = 2
EVAL_DEFECT_RATE = 0.35

_SCORING_RULES = (
    "A seeded defect counts as caught when a finding with the expected "
    "check_id lands on the labeled story; strict-path additionally requires a "
    "finding whose evidence.json_path equals a label json_path for that check "
    "on that story. Precision counts false positives ONLY on clean stories "
    "(empty label list): FP = clean stories with at least one finding for the "
    "check, precision = caught / (caught + FP). Findings on seeded stories "
    "beyond their labels are not counted as false positives — a seeded defect "
    "often legitimately trips neighbouring checks (a dead typosquat domain "
    "also fails link health), so cross-check contamination on seeded stories "
    "is excluded by design. P3 findings (which include every UNVERIFIABLE "
    "info finding) never count as false positives: they do not alert."
)


# -- scoring ------------------------------------------------------------------


@dataclass
class CheckScore:
    """Per-check tallies against the golden labels (counts are per STORY)."""

    check_id: str
    seeded: int = 0       # stories labeled with this check
    caught: int = 0       # labeled stories with >= 1 finding of this check
    strict_path: int = 0  # of caught: finding json_path equals a label path
    fp_clean: int = 0     # clean stories with >= 1 alerting (non-P3) finding

    @property
    def recall(self) -> float | None:
        return None if self.seeded == 0 else self.caught / self.seeded

    @property
    def precision(self) -> float | None:
        denom = self.caught + self.fp_clean
        return None if denom == 0 else self.caught / denom

    def as_dict(self) -> dict:
        return {
            "seeded": self.seeded,
            "caught": self.caught,
            "recall": self.recall,
            "precision": self.precision,
            "strict_path": self.strict_path,
            "fp_clean": self.fp_clean,
        }


def score_checks(labels: dict[str, list[dict]],
                 by_story: dict[str, StoryVerdict]) -> dict[str, CheckScore]:
    """Per-check recall/precision tallies. See module docstring for the rules."""
    scores: dict[str, CheckScore] = {}

    def row(check_id: str) -> CheckScore:
        return scores.setdefault(check_id, CheckScore(check_id))

    for story_id, entries in labels.items():
        verdict = by_story.get(story_id)
        findings = verdict.findings if verdict else []
        if not entries:
            # Clean story: the false-positive denominator. P3 findings do not
            # alert (UNVERIFIABLE info lands here), so they are not FPs.
            for check_id in {f.check_id for f in findings if f.severity != "P3"}:
                row(check_id).fp_clean += 1
            continue
        wanted: dict[str, set[str]] = {}
        for entry in entries:
            wanted.setdefault(entry["check_id"], set()).add(entry.get("json_path") or "")
        for check_id, label_paths in wanted.items():
            score = row(check_id)
            score.seeded += 1
            hits = [f for f in findings if f.check_id == check_id]
            if hits:
                score.caught += 1
                if any((f.evidence.json_path or "") in label_paths for f in hits):
                    score.strict_path += 1
    return scores


def score_overall(labels: dict[str, list[dict]],
                  by_story: dict[str, StoryVerdict],
                  verdicts: list[StoryVerdict]) -> dict:
    """Headline metrics: P0/P1 catch rate, clean flag rate, review load, latency, cost."""
    seeded_p01 = caught_p01 = 0
    clean_total = clean_flagged = 0
    missing_verdicts = 0
    for story_id, entries in labels.items():
        verdict = by_story.get(story_id)
        if verdict is None:
            missing_verdicts += 1
        if not entries:
            if verdict is not None:
                clean_total += 1
                if verdict.verdict != "GREEN":
                    clean_flagged += 1
            continue
        for check_id in {e["check_id"] for e in entries}:
            if DEFAULT_SEVERITY.get(check_id) in ("P0", "P1"):
                seeded_p01 += 1
                if verdict is not None and any(
                        f.check_id == check_id for f in verdict.findings):
                    caught_p01 += 1

    n = len(verdicts)
    review = sum(1 for v in verdicts if v.verdict in ("AMBER", "RED"))
    latencies = [int(v.totals.get("latency_ms", 0)) for v in verdicts]
    mean_cost = (sum(float(v.totals.get("cost_usd", 0.0)) for v in verdicts) / n
                 if n else 0.0)
    return {
        "p0_p1_seeded": seeded_p01,
        "p0_p1_caught": caught_p01,
        "p0_p1_catch_rate": (caught_p01 / seeded_p01) if seeded_p01 else None,
        "clean_stories": clean_total,
        "clean_flagged": clean_flagged,
        "clean_flag_rate": (clean_flagged / clean_total) if clean_total else None,
        "review_load": (review / n) if n else None,
        "median_latency_ms": float(statistics.median(latencies)) if latencies else 0.0,
        "mean_cost_usd": mean_cost,
        "cost_per_1k_usd": mean_cost * 1000.0,
        "verdict_count": n,
        "labeled_stories_missing_verdict": missing_verdicts,
    }


def no_coverage_checks(labels: dict[str, list[dict]]) -> list[str]:
    """Catalog checks (DEFAULT_SEVERITY keys) never exercised by any seed.

    Listed explicitly rather than silently omitted — for these, recall is
    UNKNOWN, not perfect. Expected: T2.visual_quality, T2.visual_brand,
    T2.visual_safety, T0.missing_action (05_DATA_AND_EVAL.md seed matrix).
    """
    labeled = {e["check_id"] for entries in labels.values() for e in entries}
    return sorted(c for c in DEFAULT_SEVERITY if c not in labeled)


def replay_provenance(cache_path: Path) -> dict[str, int] | None:
    """Count replay-cache entries by their "source" field; None if no cache.

    hand_authored entries demonstrate pipeline plumbing, not model skill —
    live-recorded entries replace them after a --live run.
    """
    if not cache_path.exists():
        return None
    counts: dict[str, int] = {}
    for line in cache_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        source = str(entry.get("source", "unknown"))
        counts[source] = counts.get(source, 0) + 1
    return counts


# -- formatting ---------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100.0 * x:.0f}%"


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Aligned monospace table for stdout."""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    def line(cells: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()
    sep = "  ".join("-" * w for w in widths)
    return "\n".join([line(headers), sep] + [line(r) for r in rows])


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _check_rows(scores: dict[str, CheckScore]) -> list[list[str]]:
    rows = []
    for check_id in sorted(scores):
        s = scores[check_id]
        rows.append([check_id, str(s.seeded), str(s.caught), _pct(s.recall),
                     _pct(s.precision), str(s.strict_path), str(s.fp_clean)])
    return rows


def _overall_rows(overall: dict) -> list[list[str]]:
    return [
        ["P0/P1 catch rate (seeded)",
         f"{_pct(overall['p0_p1_catch_rate'])} "
         f"({overall['p0_p1_caught']}/{overall['p0_p1_seeded']})"],
        ["Flag rate on clean stories",
         f"{_pct(overall['clean_flag_rate'])} "
         f"({overall['clean_flagged']}/{overall['clean_stories']})"],
        ["Human review load (AMBER+RED, all stories)", _pct(overall["review_load"])],
        ["Median latency / story", f"{overall['median_latency_ms']:.0f} ms"],
        ["Mean cost / story", f"${overall['mean_cost_usd']:.4f}"],
        ["Cost per 1k stories", f"${overall['cost_per_1k_usd']:.2f}"],
    ]


def _provenance_text(counts: dict[str, int] | None, cache_path: Path) -> str:
    if counts is None:
        return (f"No replay cache found at {cache_path.name} — the pipeline ran "
                "Tier 0 only (or the cache lives elsewhere).")
    total = sum(counts.values())
    parts = ", ".join(f"{counts[k]} {k}" for k in sorted(counts))
    return (f"Replay cache {cache_path.name}: {total} entries ({parts}). "
            "Hand-authored entries demonstrate pipeline plumbing, not model "
            "skill — live-recorded numbers replace them after a --live run.")


def _build_report(args: argparse.Namespace, scores: dict[str, CheckScore],
                  overall: dict, no_coverage: list[str],
                  provenance: dict[str, int] | None, cache_path: Path,
                  n_labeled: int) -> str:
    """Assemble eval/report.md — every number computed from this run."""
    check_headers = ["check_id", "seeded", "caught", "recall", "precision",
                     "strict-path", "FP (clean)"]
    lines = [
        "# Greenlight eval report",
        "",
        f"- seed: {args.seed}",
        f"- stories: {n_labeled} labeled, {overall['verdict_count']} verdicts",
        f"- tenants: {EVAL_TENANTS}, defect rate: {EVAL_DEFECT_RATE}",
        f"- date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (UTC)",
        ("- mode: live (Anthropic API, responses recorded to the replay cache)"
         if getattr(args, "live", False) else
         "- mode: replay (offline; external hosts UNVERIFIABLE by design)"),
        "",
        "## Per-check scores",
        "",
        _md_table(check_headers, _check_rows(scores)),
        "",
        "## Overall",
        "",
        _md_table(["metric", "value"], _overall_rows(overall)),
        "",
        "## Scoring rules",
        "",
        _SCORING_RULES,
        "",
        "## No eval coverage",
        "",
        "Catalog checks never exercised by a seeded defect — recall is "
        "unknown for these, not perfect:",
        "",
    ]
    lines += [f"- `{c}`" for c in no_coverage] or ["- (none)"]
    lines += ["", "## Replay provenance", "",
              _provenance_text(provenance, cache_path), ""]
    if overall["labeled_stories_missing_verdict"]:
        lines += [f"WARNING: {overall['labeled_stories_missing_verdict']} labeled "
                  "stories produced no verdict; they score as not caught.", ""]
    return "\n".join(lines)


# -- driver -------------------------------------------------------------------


def _resolve(path: str | Path) -> Path:
    """Relative CLI paths are repo-root-relative (cwd is unreliable)."""
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_eval",
        description="Seeded-defect eval: generate world, run pipeline in "
                    "replay mode, score against golden labels.")
    parser.add_argument("--seed", type=int, default=1337,
                        help="world seed (fixed default keeps runs reproducible)")
    parser.add_argument("--stories", type=int, default=40,
                        help="stories to generate across tenants")
    parser.add_argument("--port", type=int, default=8199,
                        help="local asset/CTA server port (8199 avoids the "
                             "demo feed on 8100)")
    parser.add_argument("--out", default="eval/world",
                        help="directory for the generated world")
    parser.add_argument("--report", default="eval/report.md",
                        help="markdown report output path")
    parser.add_argument("--db", default=None,
                        help="store db path; default is a fresh temp db "
                             "(never the production greenlight.db)")
    parser.add_argument("--live", action="store_true",
                        help="call the Anthropic API (records responses into "
                             "the replay cache); default is offline replay")
    return parser.parse_args(argv)


def _run_pipeline(args: argparse.Namespace, out_dir: Path,
                  db_path: Path) -> tuple[Any, list[StoryVerdict]]:
    """Generate the world, serve it, run the engine in replay mode.

    feedgen/engine are imported here (not at module top) so scoring helpers
    stay importable in unit tests without the full world stack.
    """
    from feedgen.generate import generate_world
    from feedgen.server import serve_world

    from greenlight.engine import Engine

    out_dir.mkdir(parents=True, exist_ok=True)
    # Asset/CTA URLs inside the feed embed the port, so it must match serve_world.
    world = generate_world(out_dir, seed=args.seed, stories=args.stories,
                           tenants=EVAL_TENANTS, defect_rate=EVAL_DEFECT_RATE,
                           port=args.port)
    settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
    tenants = load_tenants(REPO_ROOT / "config" / "tenants")
    store = Store(db_path=db_path,
                  audit_path=db_path.parent / "eval-audit.jsonl",
                  feedback_path=db_path.parent / "eval-feedback.jsonl")
    server = serve_world(out_dir, args.port)
    try:
        engine = Engine(settings=settings, tenants=tenants, store=store,
                        mode="live" if args.live else "replay")
        verdicts = engine.process_feed_path(str(world.feed_path))
    finally:
        server.shutdown()
        store.close()
    return world, verdicts


def main(argv: list[str] | None = None) -> dict:
    """Run the eval end to end; return the overall metrics dict (for tests)."""
    args = _parse_args(argv)
    out_dir = _resolve(args.out)
    report_path = _resolve(args.report)
    if args.db:
        db_path = _resolve(args.db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        db_path = Path(tempfile.mkdtemp(prefix="greenlight-eval-")) / "eval.db"

    world, verdicts = _run_pipeline(args, out_dir, db_path)

    labels: dict[str, list[dict]] = json.loads(
        Path(world.labels_path).read_text(encoding="utf-8"))
    by_story = {v.story_id: v for v in verdicts}

    scores = score_checks(labels, by_story)
    overall = score_overall(labels, by_story, verdicts)
    no_coverage = no_coverage_checks(labels)
    settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
    cache_path = _resolve(settings.get("replay_cache", "fixtures/ai_replay.jsonl"))
    provenance = replay_provenance(cache_path)

    check_headers = ["check_id", "seeded", "caught", "recall", "precision",
                     "strict-path", "FP (clean)"]
    print(f"\n== Greenlight eval  (seed={args.seed}, stories={len(labels)}, replay) ==\n")
    print("Per-check scores")
    print(_format_table(check_headers, _check_rows(scores)))
    print("\nOverall")
    print(_format_table(["metric", "value"], _overall_rows(overall)))
    print("\nNo eval coverage (in catalog, never seeded): "
          + (", ".join(no_coverage) if no_coverage else "(none)"))
    print(_provenance_text(provenance, cache_path))
    if overall["labeled_stories_missing_verdict"]:
        print(f"WARNING: {overall['labeled_stories_missing_verdict']} labeled "
              "stories produced no verdict; they score as not caught.")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _build_report(args, scores, overall, no_coverage, provenance,
                      cache_path, len(labels)),
        encoding="utf-8", newline="\n")
    print(f"\nReport written to {report_path}")

    result = dict(overall)
    result.update({
        "seed": args.seed,
        "stories": len(labels),
        "per_check": {cid: s.as_dict() for cid, s in sorted(scores.items())},
        "no_coverage": no_coverage,
        "replay_sources": provenance,
        "report_path": str(report_path),
    })
    return result


if __name__ == "__main__":
    # Reporting tool, not a gate: always exit 0 so CI never fails on eval
    # numbers. A crash still prints its traceback for debugging.
    try:
        main()
    except Exception:  # noqa: BLE001 — deliberate: exit code must stay 0
        traceback.print_exc()
    sys.exit(0)
