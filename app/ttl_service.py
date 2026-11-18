"""Per-service report validity windows (time-to-live).

A service with a configured TTL is *expired* when its last valid (applied)
report is older than ``ttl_seconds`` counted from the report's acceptance
time.  Expiry latches: once a report has been judged expired the fact is
persisted (``service_state.expired = 1``), so extending the window later can
never resurrect a stale report -- only a higher-seq applied report clears the
flag (see :mod:`.report_service`).  Services without a configured TTL are
judged exactly as before.
"""
from __future__ import annotations

import sqlite3
import time

from .database import read_tx, write_tx
from .errors import UnknownService


def _is_expired(row: sqlite3.Row, ttl_seconds: int, now: float) -> bool:
    """True when the last valid report is past its validity window."""
    if row["expired"]:
        return True
    if row["accepted_at"] is None:  # defensive: rows always carry a timestamp
        return False
    return now > row["accepted_at"] + ttl_seconds


def set_ttl(service: str, ttl_seconds: int) -> dict:
    """Set or replace the report validity window for one service.

    The expiry of the last valid report is judged **immediately** against its
    persisted acceptance time: any expiry that already holds (under the old
    window, under the new -- possibly shortened -- window, or via the
    persisted latch) is latched into ``service_state.expired`` inside the same
    serialized transaction, so a later window extension cannot resurrect it.
    """
    now = time.time()
    with write_tx() as conn:
        exists = conn.execute(
            "SELECT 1 FROM nodes WHERE service = ?", (service,)
        ).fetchone()
        if exists is None:
            raise UnknownService(
                f"service {service!r} is not part of the current graph"
            )

        old = conn.execute(
            "SELECT ttl_seconds FROM report_ttl WHERE service = ?", (service,)
        ).fetchone()
        state = conn.execute(
            "SELECT status, last_seq, accepted_at, expired "
            "FROM service_state WHERE service = ?",
            (service,),
        ).fetchone()

        latch = False
        if state is not None:
            if old is not None and _is_expired(state, old["ttl_seconds"], now):
                latch = True
            if _is_expired(state, ttl_seconds, now):
                latch = True

        conn.execute(
            "INSERT INTO report_ttl (service, ttl_seconds) VALUES (?, ?) "
            "ON CONFLICT(service) DO UPDATE SET ttl_seconds = excluded.ttl_seconds, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (service, ttl_seconds),
        )
        if latch and state is not None and not state["expired"]:
            conn.execute(
                "UPDATE service_state SET expired = 1 WHERE service = ?",
                (service,),
            )

    return {
        "service": service,
        "ttl_seconds": ttl_seconds,
        "previous_ttl_seconds": old["ttl_seconds"] if old else None,
        "in_graph": True,
        "reported": state is not None,
        "expired": latch,
        "last_status": state["status"] if state else None,
        "last_seq": state["last_seq"] if state else None,
        "accepted_at": state["accepted_at"] if state else None,
    }


def list_ttl() -> dict:
    """List all TTL configs, including ones whose service left the graph.

    Services removed from the graph keep their config but do not participate
    in expiry judgement (``expired`` is reported as null for them).
    """
    now = time.time()
    with read_tx() as conn:
        rows = conn.execute(
            "SELECT t.service, t.ttl_seconds, t.updated_at, "
            "s.status AS status, s.last_seq AS last_seq, "
            "s.accepted_at AS accepted_at, s.expired AS expired, "
            "n.service IS NOT NULL AS in_graph "
            "FROM report_ttl t "
            "LEFT JOIN service_state s ON s.service = t.service "
            "LEFT JOIN nodes n ON n.service = t.service "
            "ORDER BY t.service"
        ).fetchall()

    configs = []
    for r in rows:
        in_graph = bool(r["in_graph"])
        reported = r["status"] is not None
        expired = None
        if in_graph and reported:
            expired = _is_expired(r, r["ttl_seconds"], now)
        configs.append(
            {
                "service": r["service"],
                "ttl_seconds": r["ttl_seconds"],
                "in_graph": in_graph,
                "reported": reported,
                "expired": expired,
                "last_status": r["status"],
                "last_seq": r["last_seq"],
                "accepted_at": r["accepted_at"],
                "updated_at": r["updated_at"],
            }
        )
    return {"configs": configs}


def expired_service_map(conn: sqlite3.Connection, now: float) -> dict[str, dict]:
    """Expiry info for in-graph services whose last valid report aged out.

    Used by the alert classifier; reads the caller's snapshot so the result is
    consistent with the rest of one committed state.
    """
    rows = conn.execute(
        "SELECT t.service, t.ttl_seconds, s.status, s.last_seq, "
        "s.accepted_at, s.expired "
        "FROM report_ttl t "
        "JOIN service_state s ON s.service = t.service "
        "JOIN nodes n ON n.service = t.service"
    ).fetchall()
    expired: dict[str, dict] = {}
    for r in rows:
        if _is_expired(r, r["ttl_seconds"], now):
            expired[r["service"]] = {
                "service": r["service"],
                "last_status": r["status"],
                "last_seq": r["last_seq"],
                "accepted_at": r["accepted_at"],
                "ttl_seconds": r["ttl_seconds"],
                "deadline": r["accepted_at"] + r["ttl_seconds"],
            }
    return expired
