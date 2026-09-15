"""Durable audit trail.

The original build kept every decision in Streamlit session state and an
in-memory checkpointer, so a page refresh erased the record of what was decided
and by whom. For AML that record is the deliverable, not a nicety.

SQLite here for a single-node prototype. Swap the DSN for Postgres in
production and add append-only enforcement at the database level; nothing in
this module updates or deletes a row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import config

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at     TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    batch_id        TEXT    NOT NULL,
    transaction_id  TEXT    NOT NULL,
    stage           TEXT    NOT NULL,
    verdict         TEXT,
    risk_score      INTEGER,
    selection_reason TEXT,
    rule_reasons    TEXT,
    rationale       TEXT,
    model_name      TEXT,
    evidence_hash   TEXT,
    actor           TEXT,
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_txn ON audit_events(transaction_id);
CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_events(batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(occurred_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def evidence_hash(payload: Any) -> str:
    """Stable hash of what the model was shown, for later reproduction."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


@contextmanager
def _connect(db_path: Optional[Path] = None):
    path = Path(db_path or config.AUDIT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize(db_path: Optional[Path] = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def record(
    *,
    session_id: str,
    batch_id: str,
    transaction_id: str,
    stage: str,
    verdict: Optional[str] = None,
    risk_score: Optional[int] = None,
    selection_reason: Optional[str] = None,
    rule_reasons: Optional[Iterable[str]] = None,
    rationale: Optional[str] = None,
    model_name: Optional[str] = None,
    evidence: Any = None,
    actor: Optional[str] = None,
    detail: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Append one immutable event. Never raises into the caller's path."""
    try:
        with _connect(db_path) as conn:
            conn.execute(
                """INSERT INTO audit_events (
                       occurred_at, session_id, batch_id, transaction_id, stage,
                       verdict, risk_score, selection_reason, rule_reasons,
                       rationale, model_name, evidence_hash, actor, detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _now(), session_id, batch_id, str(transaction_id), stage,
                    verdict, risk_score, selection_reason,
                    json.dumps(list(rule_reasons or [])),
                    rationale, model_name,
                    evidence_hash(evidence) if evidence is not None else None,
                    actor, detail,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to write audit event for %s: %s", transaction_id, exc)


def record_many(events: List[Dict[str, Any]], db_path: Optional[Path] = None) -> None:
    for event in events:
        record(db_path=db_path, **event)


def history(transaction_id: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE transaction_id = ? ORDER BY id",
            (str(transaction_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def recent(limit: int = 200, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def batch_summary(batch_id: str, db_path: Optional[Path] = None) -> Dict[str, int]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM audit_events "
            "WHERE batch_id = ? AND stage = 'tier2_assessment' GROUP BY verdict",
            (batch_id,),
        ).fetchall()
    return {r["verdict"] or "UNKNOWN": r["n"] for r in rows}
