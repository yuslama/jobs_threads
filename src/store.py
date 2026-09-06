"""SQLite persistence (R4).

One file, no server. Holds seen posts, seen search URLs, leads and their
status, the notification retry queue, account-discovery counts, the
filtered-post log and per-source health. Everything survives a restart, which
is the whole point: a bot that re-notifies its backlog after a reboot gets
muted on day one.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_posts (
    pk           TEXT PRIMARY KEY,
    code         TEXT,
    username     TEXT NOT NULL,
    source       TEXT NOT NULL,
    caption      TEXT,
    url          TEXT,
    taken_at     TEXT,
    first_seen_at TEXT NOT NULL,
    passed_filter INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_seen_posts_username ON seen_posts(username);

CREATE TABLE IF NOT EXISTS seen_urls (
    url       TEXT PRIMARY KEY,
    query     TEXT,
    outcome   TEXT NOT NULL,          -- pending | scraped | dead | skipped
    seen_at   TEXT NOT NULL,
    post_pk   TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_urls_outcome ON seen_urls(outcome);

CREATE TABLE IF NOT EXISTS leads (
    pk          TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    source      TEXT NOT NULL,
    role        TEXT,
    email       TEXT,
    url         TEXT,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    notified_at TEXT,
    status      TEXT NOT NULL DEFAULT 'new',   -- new | applied | not_relevant
    status_at   TEXT
);

CREATE TABLE IF NOT EXISTS notify_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- lead | operator
    lead_pk    TEXT,
    payload    TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS account_hits (
    username TEXT NOT NULL,
    post_pk  TEXT NOT NULL,
    post_url TEXT,
    seen_at  TEXT NOT NULL,
    PRIMARY KEY (username, post_pk)
);

CREATE TABLE IF NOT EXISTS account_suggestions (
    username     TEXT PRIMARY KEY,
    suggested_at TEXT NOT NULL,
    hits         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS filtered_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    pk        TEXT,
    username  TEXT,
    source    TEXT,
    reason    TEXT NOT NULL,
    caption   TEXT,
    logged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_state (
    source               TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    backoff_level        INTEGER NOT NULL DEFAULT 0,
    paused_until         TEXT,
    zero_result_streak   INTEGER NOT NULL DEFAULT 0,
    last_success_at      TEXT,
    last_error           TEXT,
    alerted              INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS query_quota (
    day      TEXT NOT NULL,
    provider TEXT NOT NULL,
    used     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider)
);

CREATE TABLE IF NOT EXISTS seen_accounts (
    username    TEXT PRIMARY KEY,
    first_poll_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SourceState:
    source: str
    consecutive_failures: int = 0
    backoff_level: int = 0
    paused_until: datetime | None = None
    zero_result_streak: int = 0
    last_success_at: str | None = None
    last_error: str | None = None
    alerted: bool = False

    def is_paused(self, now: datetime | None = None) -> bool:
        if self.paused_until is None:
            return False
        return (now or datetime.now(timezone.utc)) < self.paused_until


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------- seen posts
    def is_post_seen(self, pk: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM seen_posts WHERE pk = ?", (pk,)).fetchone()
        return row is not None

    def mark_post_seen(self, post: Any, source: str, passed_filter: bool) -> bool:
        """Record a post. Returns False when it was already known.

        This is the cross-source dedupe gate: the same post reached by both
        sources is inserted once, so only the first sighting can notify.
        """
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                """INSERT OR IGNORE INTO seen_posts
                   (pk, code, username, source, caption, url, taken_at, first_seen_at, passed_filter)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    post.pk,
                    post.code,
                    post.username,
                    source,
                    post.caption,
                    post.url,
                    post.taken_at.isoformat() if getattr(post, "taken_at", None) else None,
                    _now(),
                    int(passed_filter),
                ),
            )
            inserted = cur.rowcount > 0
        self.conn.commit()
        return inserted

    def count_posts(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen_posts").fetchone()[0]

    # ------------------------------------------------------------ seen urls
    def is_url_seen(self, url: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM seen_urls WHERE url = ?", (url,)).fetchone()
        return row is not None

    def record_url(self, url: str, query: str | None, outcome: str, post_pk: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO seen_urls (url, query, outcome, seen_at, post_pk)
               VALUES (?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 outcome = excluded.outcome,
                 post_pk = COALESCE(excluded.post_pk, seen_urls.post_pk)""",
            (url, query, outcome, _now(), post_pk),
        )
        self.conn.commit()

    def new_urls(self, urls: Iterable[str]) -> list[str]:
        return [u for u in urls if not self.is_url_seen(u)]

    # ---------------------------------------------------------------- leads
    def save_lead(self, pk: str, username: str, source: str, role: str | None,
                  email: str | None, url: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO leads
               (pk, username, source, role, email, url, payload, created_at, notified_at, status, status_at)
               VALUES (?,?,?,?,?,?,?,?,
                       (SELECT notified_at FROM leads WHERE pk = ?),
                       COALESCE((SELECT status FROM leads WHERE pk = ?), 'new'),
                       (SELECT status_at FROM leads WHERE pk = ?))""",
            (pk, username, source, role, email, url, json.dumps(payload), _now(), pk, pk, pk),
        )
        self.conn.commit()

    def mark_lead_notified(self, pk: str) -> None:
        self.conn.execute("UPDATE leads SET notified_at = ? WHERE pk = ?", (_now(), pk))
        self.conn.commit()

    def set_lead_status(self, pk: str, status: str) -> bool:
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                "UPDATE leads SET status = ?, status_at = ? WHERE pk = ?", (status, _now(), pk)
            )
            changed = cur.rowcount > 0
        self.conn.commit()
        return changed

    def get_lead(self, pk: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM leads WHERE pk = ?", (pk,)).fetchone()
        return dict(row) if row else None

    def lead_stats(self, since: str | None = None) -> dict[str, int]:
        clause, params = ("WHERE created_at >= ?", (since,)) if since else ("", ())
        rows = self.conn.execute(
            f"SELECT source, status, COUNT(*) AS n FROM leads {clause} GROUP BY source, status", params
        ).fetchall()
        stats: dict[str, int] = {"total": 0}
        for row in rows:
            stats["total"] += row["n"]
            stats[f"{row['source']}:{row['status']}"] = row["n"]
            stats[row["status"]] = stats.get(row["status"], 0) + row["n"]
            stats[row["source"]] = stats.get(row["source"], 0) + row["n"]
        return stats

    # -------------------------------------------------------- notify queue
    def enqueue_notification(self, kind: str, payload: dict[str, Any], lead_pk: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO notify_queue (kind, lead_pk, payload, created_at) VALUES (?,?,?,?)",
            (kind, lead_pk, json.dumps(payload), _now()),
        )
        self.conn.commit()

    def pending_notifications(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM notify_queue ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out

    def drop_notification(self, queue_id: int) -> None:
        self.conn.execute("DELETE FROM notify_queue WHERE id = ?", (queue_id,))
        self.conn.commit()

    def bump_notification_attempt(self, queue_id: int, error: str) -> int:
        self.conn.execute(
            "UPDATE notify_queue SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            (error[:500], queue_id),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT attempts FROM notify_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        return row["attempts"] if row else 0

    # ----------------------------------------------------------- discovery
    def record_account_hit(self, username: str, post_pk: str, post_url: str) -> int:
        """Record a passing post from an account and return its distinct hit count."""
        self.conn.execute(
            "INSERT OR IGNORE INTO account_hits (username, post_pk, post_url, seen_at) VALUES (?,?,?,?)",
            (username, post_pk, post_url, _now()),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT COUNT(*) FROM account_hits WHERE username = ?", (username,)
        ).fetchone()[0]

    def account_hit_urls(self, username: str, limit: int = 5) -> list[str]:
        rows = self.conn.execute(
            "SELECT post_url FROM account_hits WHERE username = ? ORDER BY seen_at DESC LIMIT ?",
            (username, limit),
        ).fetchall()
        return [r["post_url"] for r in rows if r["post_url"]]

    def is_account_suggested(self, username: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM account_suggestions WHERE username = ?", (username,)
        ).fetchone()
        return row is not None

    def mark_account_suggested(self, username: str, hits: int) -> bool:
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                "INSERT OR IGNORE INTO account_suggestions (username, suggested_at, hits) VALUES (?,?,?)",
                (username, _now(), hits),
            )
            inserted = cur.rowcount > 0
        self.conn.commit()
        return inserted

    # -------------------------------------------------------- filtered log
    def log_filtered(self, post: Any, source: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO filtered_log (pk, username, source, reason, caption, logged_at) VALUES (?,?,?,?,?,?)",
            (post.pk, post.username, source, reason, (post.caption or "")[:1000], _now()),
        )
        self.conn.commit()

    def recent_filtered(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM filtered_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------- source health
    def get_source_state(self, source: str) -> SourceState:
        row = self.conn.execute(
            "SELECT * FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        if row is None:
            return SourceState(source=source)
        paused = None
        if row["paused_until"]:
            try:
                paused = datetime.fromisoformat(row["paused_until"])
            except ValueError:
                paused = None
        return SourceState(
            source=source,
            consecutive_failures=row["consecutive_failures"],
            backoff_level=row["backoff_level"],
            paused_until=paused,
            zero_result_streak=row["zero_result_streak"],
            last_success_at=row["last_success_at"],
            last_error=row["last_error"],
            alerted=bool(row["alerted"]),
        )

    def save_source_state(self, state: SourceState) -> None:
        self.conn.execute(
            """INSERT INTO source_state
               (source, consecutive_failures, backoff_level, paused_until,
                zero_result_streak, last_success_at, last_error, alerted)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 consecutive_failures = excluded.consecutive_failures,
                 backoff_level        = excluded.backoff_level,
                 paused_until         = excluded.paused_until,
                 zero_result_streak   = excluded.zero_result_streak,
                 last_success_at      = excluded.last_success_at,
                 last_error           = excluded.last_error,
                 alerted              = excluded.alerted""",
            (
                state.source,
                state.consecutive_failures,
                state.backoff_level,
                state.paused_until.isoformat() if state.paused_until else None,
                state.zero_result_streak,
                state.last_success_at,
                state.last_error,
                int(state.alerted),
            ),
        )
        self.conn.commit()

    # --------------------------------------------------------------- quota
    def quota_used(self, provider: str, day: str | None = None) -> int:
        day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT used FROM query_quota WHERE day = ? AND provider = ?", (day, provider)
        ).fetchone()
        return row["used"] if row else 0

    def consume_quota(self, provider: str, n: int = 1, day: str | None = None) -> int:
        day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.conn.execute(
            """INSERT INTO query_quota (day, provider, used) VALUES (?,?,?)
               ON CONFLICT(day, provider) DO UPDATE SET used = used + excluded.used""",
            (day, provider, n),
        )
        self.conn.commit()
        return self.quota_used(provider, day)

    # ------------------------------------------------------- first sighting
    def is_first_poll(self, username: str) -> bool:
        """True the first time an account is polled, and never again.

        Used to apply the age cutoff only on the initial poll, so adding an
        account does not dump its whole recent history into the chat.
        """
        with closing(self.conn.cursor()) as cur:
            cur.execute(
                "INSERT OR IGNORE INTO seen_accounts (username, first_poll_at) VALUES (?,?)",
                (username, _now()),
            )
            first = cur.rowcount > 0
        self.conn.commit()
        return first
