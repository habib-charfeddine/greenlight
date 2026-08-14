"""Stdlib HTTP server for a generated feedgen world.

Routes:
  /feed.json                    whole feed (JSON LIST of per-tenant objects)
  /feed.json?tenant=<tenant_id> single tenant feed object (404 if unknown)
  /assets/<story>/<page>.<ext>  synthesized media from the world dir (404 if
                                absent -- seeded row 1 relies on this)
  /site/nope-404                always 404 (seeded dead-CTA target, row 11)
  /site/<slug>                  landing stub: the generated HTML file if one
                                exists, else a generic tiny 200 page

The feed file is re-read on every request so --watch rewrites are visible
immediately. Files are served with correct Content-Type for .jpg/.mp4/.html.
"""
from __future__ import annotations

import json
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}
_NOT_FOUND = b"<html><body><h1>404 Not Found</h1></body></html>"


class FeedgenServer(ThreadingHTTPServer):
    """ThreadingHTTPServer bound to 127.0.0.1 with the world dir attached."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, world_dir: str | Path, port: int):
        self.world_dir = Path(world_dir)
        super().__init__(("127.0.0.1", port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server_version = "feedgen/1.0"
    server: FeedgenServer

    def log_message(self, fmt: str, *args) -> None:
        """Silence per-request logging; the demo console stays readable."""

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        if path == "/feed.json":
            self._serve_feed(parse_qs(parts.query))
        elif path.startswith("/assets/"):
            self._serve_file(self.server.world_dir / "assets",
                             path[len("/assets/"):])
        elif path == "/site/nope-404":
            self._send(404, _MIME[".html"], _NOT_FOUND)
        elif path.startswith("/site/"):
            self._serve_site(path[len("/site/"):])
        else:
            self._send(404, _MIME[".html"], _NOT_FOUND)

    # -- route handlers -----------------------------------------------------

    def _serve_feed(self, query: dict[str, list[str]]) -> None:
        feed_path = self.server.world_dir / "feed.json"
        if not feed_path.is_file():
            self._send(404, _MIME[".html"], _NOT_FOUND)
            return
        body = feed_path.read_bytes()
        tenant_ids = query.get("tenant")
        if tenant_ids:
            feeds = json.loads(body.decode("utf-8"))
            for feed in feeds:
                if feed.get("tenant_id") == tenant_ids[0]:
                    body = json.dumps(feed, indent=2,
                                      ensure_ascii=False).encode("utf-8")
                    break
            else:
                self._send(404, _MIME[".html"], _NOT_FOUND)
                return
        self._send(200, _MIME[".json"], body)

    def _serve_file(self, root: Path, rel: str) -> None:
        try:
            target = (root / rel).resolve()
            root = root.resolve()
        except OSError:
            self._send(404, _MIME[".html"], _NOT_FOUND)
            return
        if not target.is_relative_to(root) or not target.is_file():
            self._send(404, _MIME[".html"], _NOT_FOUND)
            return
        ctype = _MIME.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, ctype, target.read_bytes())

    def _serve_site(self, slug: str) -> None:
        slug = slug.strip("/")
        site_dir = self.server.world_dir / "site"
        page = site_dir / f"{slug}.html"
        try:
            served = page.resolve().is_relative_to(site_dir.resolve()) \
                and page.is_file()
        except OSError:
            served = False
        if served:
            self._send(200, _MIME[".html"], page.read_bytes())
        else:
            body = (f"<html><body><h1>{escape(slug) or 'home'}</h1>"
                    f"<p>Feedgen landing stub.</p></body></html>").encode("utf-8")
            self._send(200, _MIME[".html"], body)

    # -- plumbing -----------------------------------------------------------

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_world(world_dir: str | Path, port: int) -> FeedgenServer:
    """Start serving in a daemon thread; caller calls ``server.shutdown()``."""
    server = FeedgenServer(world_dir, port)
    thread = threading.Thread(target=server.serve_forever,
                              name=f"feedgen:{port}", daemon=True)
    thread.start()
    return server


def run_forever(world_dir: str | Path, port: int) -> None:
    """Serve in the foreground until Ctrl+C."""
    server = FeedgenServer(world_dir, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
