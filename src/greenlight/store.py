"""SQLite persistence + JSONL audit log.

One latest verdict per story lives in SQLite (dashboard reads it); every verdict
ever produced is also appended to ``audit.jsonl`` (the immutable audit trail).
WAL mode so the watcher process can write while the dashboard process reads.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import StoryVerdict, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    story_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    prompt_version TEXT,
    verdict TEXT NOT NULL,
    score INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    findings_json TEXT NOT NULL,
    story_json TEXT NOT NULL,
    escalated_by TEXT,
    headline TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS judged (
    content_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    PRIMARY KEY (content_hash, policy_version, prompt_version)
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    check_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('confirm', 'dismiss')),
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_hashes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    url TEXT NOT NULL,
    phash TEXT NOT NULL,
    story_id TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS titles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    story_id TEXT NOT NULL,
    title TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdicts_tenant ON verdicts (tenant_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_asset_hashes_tenant ON asset_hashes (tenant_id, id);
CREATE INDEX IF NOT EXISTS idx_titles_tenant ON titles (tenant_id, ts);
"""


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if "findings_json" in d:
        d["findings"] = json.loads(d.pop("findings_json"))
    if "story_json" in d:
        d["story"] = json.loads(d.pop("story_json"))
    return d


class Store:
    def __init__(self, db_path: str | Path = "greenlight.db",
                 audit_path: str | Path = "audit.jsonl",
                 feedback_path: str | Path = "feedback.jsonl"):
        self.db_path = str(db_path)
        self.audit_path = Path(audit_path)
        self.feedback_path = Path(feedback_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- verdict lifecycle --------------------------------------------------

    def has_verdict(self, content_hash: str, policy_version: str,
                    prompt_version: str = "") -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM judged WHERE content_hash=? AND policy_version=? AND prompt_version=?",
            (content_hash, policy_version, prompt_version or ""),
        )
        return cur.fetchone() is not None

    def save_verdict(self, v: StoryVerdict, story_json: dict,
                     escalated_by: str | None = None,
                     prompt_version: str = "") -> None:
        payload = v.model_dump(mode="json")
        with self._lock:
            self._conn.execute(
                """INSERT INTO verdicts (story_id, tenant_id, content_hash, policy_version,
                       prompt_version, verdict, score, checked_at, pipeline_version,
                       cost_usd, latency_ms, findings_json, story_json, escalated_by,
                       headline)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(story_id) DO UPDATE SET
                       tenant_id=excluded.tenant_id, content_hash=excluded.content_hash,
                       policy_version=excluded.policy_version, prompt_version=excluded.prompt_version,
                       verdict=excluded.verdict, score=excluded.score,
                       checked_at=excluded.checked_at, pipeline_version=excluded.pipeline_version,
                       cost_usd=excluded.cost_usd, latency_ms=excluded.latency_ms,
                       findings_json=excluded.findings_json, story_json=excluded.story_json,
                       escalated_by=excluded.escalated_by, headline=excluded.headline""",
                (
                    v.story_id, v.tenant_id, v.content_hash,
                    payload["findings"][0]["policy_version"] if payload["findings"] else "",
                    prompt_version, v.verdict, v.score, payload["checked_at"],
                    v.pipeline_version, v.totals.get("cost_usd", 0.0),
                    v.totals.get("latency_ms", 0), json.dumps(payload["findings"]),
                    json.dumps(story_json), escalated_by,
                    str(v.totals.get("headline") or ""),
                ),
            )
            self._conn.execute(
                """INSERT OR REPLACE INTO judged (content_hash, policy_version, prompt_version, checked_at)
                   VALUES (?,?,?,?)""",
                (v.content_hash,
                 payload["findings"][0]["policy_version"] if payload["findings"] else "",
                 prompt_version, payload["checked_at"]),
            )
            self._conn.commit()
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "verdict", "escalated_by": escalated_by,
                                    **payload}) + "\n")

    def mark_judged(self, content_hash: str, policy_version: str,
                    prompt_version: str = "") -> None:
        """Record (hash, policy, prompt) even when no verdict row changes."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO judged (content_hash, policy_version, prompt_version, checked_at)
                   VALUES (?,?,?,?)""",
                (content_hash, policy_version, prompt_version or "", utcnow().isoformat()),
            )
            self._conn.commit()

    # -- dashboard queries --------------------------------------------------

    def latest_verdicts(self, tenant_id: str | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT * FROM verdicts"
        args: list = []
        if tenant_id:
            q += " WHERE tenant_id=?"
            args.append(tenant_id)
        q += " ORDER BY checked_at DESC LIMIT ?"
        args.append(limit)
        return [_row_to_dict(r) for r in self._conn.execute(q, args)]

    def get_verdict(self, story_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM verdicts WHERE story_id=?", (story_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def counters_today(self) -> dict:
        today = utcnow().date().isoformat()
        row = self._conn.execute(
            """SELECT
                 SUM(CASE WHEN verdict='GREEN' THEN 1 ELSE 0 END) AS green,
                 SUM(CASE WHEN verdict='AMBER' THEN 1 ELSE 0 END) AS amber,
                 SUM(CASE WHEN verdict='RED' THEN 1 ELSE 0 END) AS red,
                 COALESCE(SUM(cost_usd), 0) AS cost_usd
               FROM verdicts WHERE substr(checked_at, 1, 10) = ?""",
            (today,),
        ).fetchone()
        return {
            "green": row["green"] or 0,
            "amber": row["amber"] or 0,
            "red": row["red"] or 0,
            "cost_usd": row["cost_usd"] or 0.0,
            "last_poll": self.get_meta("last_poll"),
        }

    def tenant_ids(self) -> list[str]:
        return [r["tenant_id"] for r in
                self._conn.execute("SELECT DISTINCT tenant_id FROM verdicts")]

    # -- feedback -----------------------------------------------------------

    def save_feedback(self, story_id: str, check_id: str, action: str) -> None:
        ts = utcnow().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO feedback (story_id, check_id, action, ts) VALUES (?,?,?,?)",
                (story_id, check_id, action, ts),
            )
            self._conn.commit()
            with open(self.feedback_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"story_id": story_id, "check_id": check_id,
                                    "action": action, "ts": ts}) + "\n")

    def get_feedback(self, story_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT check_id, action, ts FROM feedback WHERE story_id=? ORDER BY id",
            (story_id,),
        )]

    def feedback_stats(self) -> list[dict]:
        """Per-check confirm/dismiss counts — 'what we'd auto-quiet next'."""
        rows = self._conn.execute(
            """SELECT check_id,
                 SUM(CASE WHEN action='confirm' THEN 1 ELSE 0 END) AS confirms,
                 SUM(CASE WHEN action='dismiss' THEN 1 ELSE 0 END) AS dismisses
               FROM feedback GROUP BY check_id ORDER BY dismisses DESC"""
        ).fetchall()
        out = []
        for r in rows:
            total = (r["confirms"] or 0) + (r["dismisses"] or 0)
            out.append({
                "check_id": r["check_id"],
                "confirms": r["confirms"] or 0,
                "dismisses": r["dismisses"] or 0,
                "dismiss_rate": (r["dismisses"] or 0) / total if total else 0.0,
            })
        return out

    # -- duplicate-detection memory ----------------------------------------

    def add_asset_hash(self, tenant_id: str, url: str, phash: str, story_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO asset_hashes (tenant_id, url, phash, story_id, ts) VALUES (?,?,?,?,?)",
                (tenant_id, url, phash, story_id, utcnow().isoformat()),
            )
            self._conn.commit()

    def recent_asset_hashes(self, tenant_id: str, limit: int = 500) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            """SELECT url, phash, story_id FROM asset_hashes
               WHERE tenant_id=? ORDER BY id DESC LIMIT ?""",
            (tenant_id, limit),
        )]

    def add_title(self, tenant_id: str, story_id: str, title: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO titles (tenant_id, story_id, title, ts) VALUES (?,?,?,?)",
                (tenant_id, story_id, title, utcnow().isoformat()),
            )
            self._conn.commit()

    def recent_titles(self, tenant_id: str, days: int = 7) -> list[dict]:
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        return [dict(r) for r in self._conn.execute(
            "SELECT story_id, title, ts FROM titles WHERE tenant_id=? AND ts >= ?",
            (tenant_id, cutoff),
        )]

    # -- cursors + meta -----------------------------------------------------

    def get_cursor(self, tenant_id: str) -> str | None:
        return self.get_meta(f"cursor:{tenant_id}")

    def set_cursor(self, tenant_id: str, value: str) -> None:
        self.set_meta(f"cursor:{tenant_id}", value)

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value)
            )
            self._conn.commit()

    def set_last_poll(self) -> None:
        self.set_meta("last_poll", utcnow().isoformat())

    # -- tenant health ------------------------------------------------------

    def tenant_health(self, tenant_id: str, limit: int = 200) -> dict:
        rows = self.latest_verdicts(tenant_id, limit)
        n = len(rows)
        greens = sum(1 for r in rows if r["verdict"] == "GREEN")
        review = sum(1 for r in rows if r["verdict"] in ("AMBER", "RED"))
        check_counts: dict[str, int] = {}
        for r in rows:
            for f in r["findings"]:
                check_counts[f["check_id"]] = check_counts.get(f["check_id"], 0) + 1
        cost = sum(r["cost_usd"] for r in rows)
        return {
            "tenant_id": tenant_id,
            "n": n,
            "scores": [r["score"] for r in reversed(rows)],   # oldest -> newest for the trend
            "pass_rate": greens / n if n else 1.0,
            "review_load": review / n if n else 0.0,
            "top_checks": sorted(check_counts.items(), key=lambda kv: -kv[1])[:8],
            "cost_per_1k": (cost / n * 1000) if n else 0.0,
            "policy_version": rows[0]["policy_version"] if rows else "-",
        }

    def close(self) -> None:
        self._conn.close()
