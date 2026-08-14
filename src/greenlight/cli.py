"""CLI: greenlight ingest | watch | serve | eval | demo.

Everything runs from the repo root. Replay mode is the default — zero API keys,
zero network beyond localhost. ``--live`` requires ANTHROPIC_API_KEY and records
judge responses into the replay cache.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from .policy import load_settings, load_tenants
from .store import Store

log = logging.getLogger("greenlight")


def _mode(args) -> str:
    if getattr(args, "live", False):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("--live needs ANTHROPIC_API_KEY set. Replay mode (the default) "
                     "runs fully offline from fixtures/ai_replay.jsonl.")
        return "live"
    return "replay"


def _engine(args):
    from .engine import Engine  # deferred: checks import cv2 etc.

    settings = load_settings(args.config)
    tenants = load_tenants(args.tenants_dir)
    store = Store(args.db, settings.get("audit_log", "audit.jsonl"),
                  settings.get("feedback_log", "feedback.jsonl"))
    return Engine(settings, tenants, store, mode=_mode(args)), settings, store


def cmd_ingest(args) -> None:
    engine, _, _ = _engine(args)
    verdicts = engine.process_feed_path(args.feed)
    log.info("ingest done: %d new verdicts", len(verdicts))


def cmd_watch(args) -> None:
    from .ingest import FeedError

    engine, settings, _ = _engine(args)
    feed = args.feed or settings.get("feed", {}).get("url")
    interval = args.interval or settings.get("feed", {}).get("poll_interval_s", 20)
    log.info("watching %s every %ss (Ctrl+C to stop)", feed, interval)
    while True:
        try:
            engine.process_feed_path(feed)
        except FeedError as e:
            log.error("feed error (keeping last good cursor): %s", e)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 — the watcher must survive anything
            log.exception("poll cycle failed (continuing): %s", e)
        time.sleep(float(interval))


def cmd_serve(args) -> None:
    import uvicorn

    from .server import create_app

    settings = load_settings(args.config)
    tenants = load_tenants(args.tenants_dir)
    store = Store(args.db, settings.get("audit_log", "audit.jsonl"),
                  settings.get("feedback_log", "feedback.jsonl"))
    port = args.port or settings.get("dashboard", {}).get("port", 8000)
    uvicorn.run(create_app(store, settings, tenants), host="127.0.0.1", port=int(port),
                log_level="warning")


def cmd_eval(args) -> None:
    cmd = [sys.executable, "eval/run_eval.py"]
    if args.live:
        cmd.append("--live")
    raise SystemExit(subprocess.call(cmd))


def cmd_demo(args) -> None:
    """feedgen (assets+server, watch mode) + watcher + dashboard — one process each."""
    py = sys.executable
    procs: list[subprocess.Popen] = []

    def spawn(argv: list[str]) -> None:
        procs.append(subprocess.Popen(argv))

    try:
        spawn([py, "-m", "feedgen", "--seed", "42", "--stories", str(args.stories),
               "--port", "8100", "--watch", "--interval", "15"])
        log.info("feedgen starting on :8100 — waiting for the world to build...")
        time.sleep(4)

        # The brief's own fixture goes through the same pipeline as everything else.
        # NOTE: --db is a GLOBAL option and must precede the subcommand.
        fixture = Path("fixtures/example_from_brief.json")
        if fixture.exists():
            subprocess.call([py, "-m", "greenlight.cli", "--db", args.db,
                             "ingest", "--feed", str(fixture)])

        spawn([py, "-m", "greenlight.cli", "--db", args.db, "watch", "--feed",
               "http://localhost:8100/feed.json", "--interval", "20"])
        spawn([py, "-m", "greenlight.cli", "--db", args.db, "serve", "--port", "8000"])
        log.info("demo up: dashboard http://localhost:8000  feed http://localhost:8100/feed.json")
        log.info("Ctrl+C stops everything.")
        while True:
            time.sleep(1)
            dead = [p for p in procs if p.poll() is not None]
            if dead:
                for p in dead:
                    log.error("child %s exited with %s — stopping the demo",
                              " ".join(map(str, p.args[-4:])), p.returncode)
                break
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(prog="greenlight",
                                 description="Publish-confidence layer for story feeds")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--tenants-dir", default="config/tenants")
    ap.add_argument("--db", default="greenlight.db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="one poll cycle over a feed url/path")
    p.add_argument("--feed", required=True)
    p.add_argument("--replay", action="store_true", help="(default) offline, replay AI cache")
    p.add_argument("--live", action="store_true", help="call the Anthropic API and record")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("watch", help="continuous polling loop")
    p.add_argument("--feed", default=None)
    p.add_argument("--interval", type=float, default=None)
    p.add_argument("--replay", action="store_true")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("serve", help="triage dashboard")
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("eval", help="seeded-defect eval -> eval/report.md + stdout")
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("demo", help="feedgen + watch + serve")
    p.add_argument("--stories", type=int, default=40)
    p.set_defaults(fn=cmd_demo)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
