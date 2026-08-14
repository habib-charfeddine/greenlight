"""feedgen: deterministic mock Storyteller feed + assets + HTTP server.

Standalone from the ``greenlight`` package on purpose: the generator is the
eval's ground truth, so it must not share code with the system under test.
"""
from .generate import WorldInfo, append_batch, generate_world
from .server import FeedgenServer, run_forever, serve_world

__all__ = [
    "WorldInfo",
    "append_batch",
    "generate_world",
    "FeedgenServer",
    "run_forever",
    "serve_world",
]
