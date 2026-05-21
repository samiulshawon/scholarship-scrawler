"""Session-state management.

The crawler is long-running and pause/resume must be exact. We persist the
queue, current cursor, visited set, and counters to a JSON file so a process
restart picks up where it left off, and the DB stays the source of truth for
results.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


def make_session_id(now: datetime | None = None) -> str:
    """Timestamp-based session ID, format YYYYMMDD_HHMMSS."""
    return (now or datetime.utcnow()).strftime("%Y%m%d_%H%M%S")


class SessionState:
    """Thread-safe JSON-backed session state.

    Layout::

        {
          "session_id": "20260521_120000",
          "started_at": "...iso...",
          "current_country": "Germany",
          "current_phase": "university",
          "queue": [["Germany", "university", "https://..."], ...],
          "cursor": 0,
          "visited": ["https://...", ...],
          "pages_visited": 12,
          "records_found": 4,
          "paused": false,
          "logs": [{"level": "info", "ts": "...", "msg": "..."}, ...]
        }
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = self._load()

    # -------------------------------------------------------------------- io
    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # Corrupt state — back it up and start fresh.
                self.path.rename(self.path.with_suffix(".corrupt.json"))
        return {}

    def save(self) -> None:
        with self._lock:
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    # ----------------------------------------------------------- lifecycle
    def init_session(self, session_id: str, queue: list[tuple[str, str, str]]) -> None:
        with self._lock:
            self.data = {
                "session_id": session_id,
                "started_at": datetime.utcnow().isoformat(timespec="seconds"),
                "current_country": queue[0][0] if queue else None,
                "current_phase": queue[0][1] if queue else None,
                "queue": [list(item) for item in queue],
                "cursor": 0,
                "visited": [],
                "pages_visited": 0,
                "records_found": 0,
                "paused": False,
                "logs": [],
            }
            self.save()

    def resume(self, queue: list[tuple[str, str, str]]) -> None:
        """Merge a fresh queue with previously visited URLs to skip duplicates."""
        with self._lock:
            visited = set(self.data.get("visited", []))
            remaining = [list(item) for item in queue if item[2] not in visited]
            self.data["queue"] = remaining
            self.data["cursor"] = 0
            self.data["paused"] = False
            self.save()

    # ------------------------------------------------------------- mutators
    def request_pause(self) -> None:
        with self._lock:
            self.data["paused"] = True
            self.save()

    def is_paused(self) -> bool:
        with self._lock:
            return bool(self.data.get("paused"))

    def advance(self, url: str) -> None:
        with self._lock:
            self.data["visited"] = list({*self.data.get("visited", []), url})
            self.data["cursor"] = self.data.get("cursor", 0) + 1
            self.data["pages_visited"] = self.data.get("pages_visited", 0) + 1
            self.save()

    def increment_records(self, n: int = 1) -> None:
        with self._lock:
            self.data["records_found"] = self.data.get("records_found", 0) + n
            self.save()

    def set_position(self, country: str, phase: str) -> None:
        with self._lock:
            self.data["current_country"] = country
            self.data["current_phase"] = phase
            self.save()

    def log(self, level: str, msg: str) -> None:
        """Append a single log line; capped at 1000 entries to bound disk use."""
        with self._lock:
            logs = self.data.setdefault("logs", [])
            logs.append({
                "level": level,
                "ts": datetime.utcnow().isoformat(timespec="seconds"),
                "msg": msg,
            })
            if len(logs) > 1000:
                self.data["logs"] = logs[-1000:]
            self.save()

    # ---------------------------------------------------------------- views
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.data))

    @property
    def session_id(self) -> str | None:
        return self.data.get("session_id")
