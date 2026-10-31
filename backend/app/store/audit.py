"""Persistent incident records. Specification section 07 ("Audit + evaluation").

Plain SQLite with JSON payloads: the demo must survive a restart and the record
must be inspectable after the event without the server running.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from ..config import settings

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id   TEXT PRIMARY KEY,
    scenario_key  TEXT,
    status        TEXT,
    state         TEXT,
    confidence    REAL,
    diagnosis     TEXT,
    provider      TEXT,
    created_at    REAL,
    closed_at     REAL,
    duration_s    REAL,
    tool_calls    INTEGER,
    iterations    INTEGER,
    payload       TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at DESC);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(settings.db_path, timeout=10.0)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _LOCK, _conn() as c:
        c.executescript(SCHEMA)


def save_incident(summary: dict[str, Any]) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO incidents
               (incident_id, scenario_key, status, state, confidence, diagnosis,
                provider, created_at, closed_at, duration_s, tool_calls,
                iterations, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (summary.get("incident_id"), summary.get("scenario_key"),
             summary.get("status"), summary.get("state"),
             summary.get("confidence", 0.0), summary.get("diagnosis", ""),
             summary.get("provider", ""), summary.get("created_at"),
             summary.get("closed_at"), summary.get("duration_s", 0.0),
             summary.get("tool_calls", 0), summary.get("iterations", 0),
             json.dumps(summary, default=str)))


def list_incidents(limit: int = 60) -> list[dict[str, Any]]:
    with _LOCK, _conn() as c:
        rows = c.execute(
            """SELECT incident_id, scenario_key, status, state, confidence,
                      diagnosis, provider, created_at, closed_at, duration_s,
                      tool_calls, iterations
               FROM incidents ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_incident(incident_id: str) -> dict[str, Any] | None:
    with _LOCK, _conn() as c:
        row = c.execute("SELECT payload FROM incidents WHERE incident_id = ?",
                        (incident_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def clear() -> int:
    with _LOCK, _conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()["n"]
        c.execute("DELETE FROM incidents")
    return n
