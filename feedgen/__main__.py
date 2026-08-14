"""CLI entry point: ``python -m feedgen``.

Generates a deterministic mock world, then serves it. ``--watch`` appends
1-3 new stories every ``--interval`` seconds (proving "the feed updates
continuously"); ``--generate-only`` writes the world and exits.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .generate import append_batch, generate_world
from .server import run_forever, serve_world


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feedgen",
        description="Mock Storyteller feed generator + asset server",
    )
    parser.add_argument("--tenants", type=int, default=2,
                        help="number of tenants (1 or 2, default 2)")
    parser.add_argument("--stories", type=int, default=40,
                        help="total stories across all tenants (default 40)")
    parser.add_argument("--defect-rate", type=float, default=0.35,
                        help="approximate share of stories carrying seeds")
    parser.add_argument("--seed", type=int, default=42,
                        help="world seed (default 42)")
    parser.add_argument("--port", type=int, default=8100,
                        help="HTTP port (default 8100)")
    parser.add_argument("--out", default="feedgen/out",
                        help="world output directory (default feedgen/out)")
    parser.add_argument("--watch", action="store_true",
                        help="append new stories every --interval seconds")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="--watch append interval in seconds (default 10)")
    parser.add_argument("--generate-only", action="store_true",
                        help="write the world and exit without serving")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    out = Path(args.out)
    world = generate_world(out, seed=args.seed, stories=args.stories,
                           tenants=args.tenants, defect_rate=args.defect_rate,
                           port=args.port)

    labels = json.loads(world.labels_path.read_text(encoding="utf-8"))
    seeded = sum(1 for entries in labels.values() if entries)
    print(f"world: {out.resolve()}")
    print(f"stories: {len(labels)} ({seeded} seeded, "
          f"{len(labels) - seeded} clean)")
    print(f"feed:   {world.feed_path}")
    print(f"labels: {world.labels_path}")
    if args.generate_only:
        return

    print(f"serving http://localhost:{args.port}/feed.json (Ctrl+C to stop)")
    if args.watch:
        server = serve_world(out, args.port)
        batch = 0
        try:
            while True:
                time.sleep(args.interval)
                batch += 1
                added = append_batch(world, seed=args.seed, batch_index=batch,
                                     port=args.port)
                print(f"[watch] appended {len(added)} stories: "
                      f"{', '.join(added)}")
        except KeyboardInterrupt:
            server.shutdown()
    else:
        run_forever(out, args.port)


if __name__ == "__main__":
    main()
