"""SQLite persistence layer for Scholarship Scout Pro.

Tables
------
scholarships     One row per discovered scholarship (deduped on application_url).
visited_urls     Every URL the crawler has fetched; powers resume + dedup.
sessions         Session lifecycle metadata (start, end, counts, status).
domain_blocks    Per-domain block counter (used to abandon hostile domains).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS scholarships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_url TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    provider TEXT,
    country TEXT,
    region TEXT,
    funding_tier TEXT,
    stipend_details TEXT,
    tuition_coverage TEXT,
    travel_allowance TEXT,
    housing_support TEXT,
    work_opportunity TEXT,
    eligible_fields TEXT,
    bangladesh_eligible TEXT,
    english_program TEXT,
    intake TEXT,
    application_deadline TEXT,
    source_page TEXT,
    verification_status TEXT,
    confidence_score INTEGER,
    review_notes TEXT,
    session_id TEXT,
    last_verified TEXT,
    raw_payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_scholarships_country ON scholarships(country);
CREATE INDEX IF NOT EXISTS idx_scholarships_tier ON scholarships(funding_tier);
CREATE INDEX IF NOT EXISTS idx_scholarships_status ON scholarships(verification_status);

CREATE TABLE IF NOT EXISTS visited_urls (
    url TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    country TEXT,
    phase TEXT,
    visited_at TEXT NOT NULL,
    http_status INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT,
    countries_scanned TEXT,
    pages_visited INTEGER DEFAULT 0,
    records_found INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS domain_blocks (
    domain TEXT PRIMARY KEY,
    block_count INTEGER DEFAULT 0,
    last_blocked_at TEXT,
    abandoned INTEGER DEFAULT 0
);
"""


class Database:
    """Thin synchronous wrapper around sqlite3.

    Streamlit and the asyncio crawler both touch the DB; SQLite handles the
    concurrency fine for this workload. Heavy/long-running calls happen in the
    crawler thread, the UI only reads.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ sessions
    def start_session(self, session_id: str, start_time: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions(session_id, start_time, status) "
                "VALUES (?, ?, 'running')",
                (session_id, start_time),
            )

    def end_session(
        self,
        session_id: str,
        end_time: str,
        countries: Iterable[str],
        pages: int,
        records: int,
        status: str = "completed",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET end_time=?, countries_scanned=?, "
                "pages_visited=?, records_found=?, status=? WHERE session_id=?",
                (end_time, ", ".join(sorted(set(countries))), pages, records, status, session_id),
            )

    def get_sessions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM sessions ORDER BY start_time DESC")]

    # ----------------------------------------------------------------- visited
    def mark_visited(
        self,
        url: str,
        session_id: str,
        country: str | None,
        phase: str | None,
        visited_at: str,
        http_status: int | None = None,
        notes: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO visited_urls(url, session_id, country, phase, "
                "visited_at, http_status, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (url, session_id, country, phase, visited_at, http_status, notes),
            )

    def is_visited(self, url: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("SELECT 1 FROM visited_urls WHERE url=?", (url,))
            return cur.fetchone() is not None

    def visited_urls(self) -> set[str]:
        with self.connect() as conn:
            return {r[0] for r in conn.execute("SELECT url FROM visited_urls")}

    # ------------------------------------------------------------- scholarships
    def upsert_scholarship(self, record: dict[str, Any]) -> None:
        """Insert or update a scholarship; uniqueness key is application_url."""
        cols = [
            "application_url", "name", "provider", "country", "region",
            "funding_tier", "stipend_details", "tuition_coverage",
            "travel_allowance", "housing_support", "work_opportunity",
            "eligible_fields", "bangladesh_eligible", "english_program",
            "intake", "application_deadline", "source_page",
            "verification_status", "confidence_score", "review_notes",
            "session_id", "last_verified", "raw_payload",
        ]
        values = [record.get(c) for c in cols]
        # Serialise list-valued fields.
        for i, c in enumerate(cols):
            if isinstance(values[i], (list, dict)):
                values[i] = json.dumps(values[i], ensure_ascii=False)
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "application_url")
        sql = (
            f"INSERT INTO scholarships({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(application_url) DO UPDATE SET {updates}"
        )
        with self.connect() as conn:
            conn.execute(sql, values)

    def fetch_scholarships(
        self,
        statuses: Iterable[str] | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if statuses:
            where.append(f"verification_status IN ({','.join('?' * len(list(statuses)))})")
            params.extend(statuses)
        if session_id:
            where.append("session_id=?")
            params.append(session_id)
        sql = "SELECT * FROM scholarships"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    # --------------------------------------------------------------- domain blocks
    def record_block(self, domain: str, when: str) -> int:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO domain_blocks(domain, block_count, last_blocked_at) "
                "VALUES (?, 1, ?) "
                "ON CONFLICT(domain) DO UPDATE SET "
                "block_count=block_count+1, last_blocked_at=excluded.last_blocked_at",
                (domain, when),
            )
            cur = conn.execute("SELECT block_count FROM domain_blocks WHERE domain=?", (domain,))
            return cur.fetchone()[0]

    def abandon_domain(self, domain: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE domain_blocks SET abandoned=1 WHERE domain=?", (domain,))

    def is_abandoned(self, domain: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "SELECT abandoned FROM domain_blocks WHERE domain=?", (domain,)
            )
            row = cur.fetchone()
            return bool(row and row[0])
