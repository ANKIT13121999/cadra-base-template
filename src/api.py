"""FastAPI service for Larkfield Home ticket decisions.

The service only proposes decisions. It never issues a refund and never sends
an email.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from schema.response_schema import Decision, Ticket
from src.extract import extract_ticket
from src.llm import LLMClient
from src.normalize import ticket_dedupe_key
from src.pipeline import process_ticket
from src.repo import PreparedData

LARKFIELD_DB_PATH = Path(os.getenv("DB_PATH") or os.getenv("LARKFIELD_DB_PATH") or "build/runs.db")
PREPARED_DB_PATH = Path(os.getenv("LARKFIELD_PREPARED_DB", "build/prepared.db"))

app = FastAPI(title="Larkfield Home support agent", version="1.0")
_write_lock = threading.RLock()
_data: PreparedData | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    LARKFIELD_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(LARKFIELD_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL UNIQUE,
                dedupe_key TEXT NOT NULL UNIQUE,
                response_json TEXT NOT NULL,
                outcome TEXT NOT NULL,
                ticket_type TEXT NOT NULL,
                human_approval_required INTEGER NOT NULL,
                approver_role TEXT,
                approval_status TEXT NOT NULL,
                refundable_amount REAL,
                latency_ms INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ledger (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                ticket_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('REFUND', 'REPLY', 'ESCALATION')),
                amount_usd REAL,
                status TEXT NOT NULL CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED')),
                recorded_at TEXT NOT NULL,
                decided_at TEXT,
                approved_by TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_runs_ticket ON runs(ticket_id);
            CREATE INDEX IF NOT EXISTS idx_runs_dedupe ON runs(dedupe_key);
            CREATE INDEX IF NOT EXISTS idx_ledger_status ON ledger(status);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _ensure_prepared() -> None:
    if PREPARED_DB_PATH.is_file():
        return
    from src.prepare import main as prepare_main

    prepare_main(["--out-dir", str(PREPARED_DB_PATH.parent)])


def _get_data() -> PreparedData:
    global _data
    if _data is None:
        _ensure_prepared()
        _data = PreparedData.from_sqlite(PREPARED_DB_PATH)
    return _data


def _dedupe_key(ticket: dict[str, Any]) -> str:
    extraction = extract_ticket(ticket, use_llm=False)
    return ticket_dedupe_key(extraction.original_sender, extraction.original_subject, str(ticket.get("body") or ""))


def _ledger_action(response: dict[str, Any], refundable_amount: float | None) -> tuple[str, float | None]:
    outcome = response["outcome"]
    if outcome == "ESCALATE":
        return "ESCALATION", None
    if outcome in {"PROCEED", "PROCEED_MODIFIED", "PEND"} and refundable_amount is not None:
        return "REFUND", round(float(refundable_amount), 2)
    return "REPLY", None


def _approval_status(response: dict[str, Any]) -> str:
    return "PENDING" if response.get("human_approval_required") else "NOT_REQUIRED"


def _fetch_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()


def _response_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["response_json"])


def _find_existing(conn: sqlite3.Connection, ticket_id: str, dedupe_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE ticket_id = ? OR dedupe_key = ? LIMIT 1",
        (ticket_id, dedupe_key),
    ).fetchone()


def _insert_run_and_ledger(
    conn: sqlite3.Connection,
    response: dict[str, Any],
    dedupe_key: str,
    refundable_amount: float | None,
) -> None:
    now = _utc_now()
    action, amount = _ledger_action(response, refundable_amount)
    conn.execute(
        """INSERT INTO runs (
            run_id, ticket_id, dedupe_key, response_json, outcome, ticket_type,
            human_approval_required, approver_role, approval_status, refundable_amount,
            latency_ms, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            response["run_id"],
            response["ticket_id"],
            dedupe_key,
            json.dumps(response, sort_keys=True),
            response["outcome"],
            response["ticket_type"],
            1 if response["human_approval_required"] else 0,
            response.get("approver_role"),
            _approval_status(response),
            amount,
            response.get("latency_ms"),
            now,
            now,
        ),
    )
    conn.execute(
        """INSERT INTO ledger (
            run_id, ticket_id, action, amount_usd, status, recorded_at
        ) VALUES (?, ?, ?, ?, 'PROPOSED', ?)""",
        (response["run_id"], response["ticket_id"], action, amount, now),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tickets")
def create_ticket(ticket: Ticket) -> dict[str, Any]:
    payload = ticket.model_dump(by_alias=True)
    ticket_id = str(payload.get("ticket_id") or "")
    dedupe_key = _dedupe_key(payload)
    with _write_lock:
        conn = _connect()
        try:
            existing = _find_existing(conn, ticket_id, dedupe_key)
            if existing is not None:
                return _response_from_row(existing)
            data = _get_data()
            result = process_ticket(payload, data, llm=LLMClient())
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = _find_existing(conn, ticket_id, dedupe_key)
                if existing is not None:
                    conn.rollback()
                    return _response_from_row(existing)
                _insert_run_and_ledger(
                    conn,
                    result.response,
                    dedupe_key,
                    result.decision.get("refundable_amount"),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                existing = _find_existing(conn, ticket_id, dedupe_key)
                if existing is not None:
                    return _response_from_row(existing)
                raise
            return result.response
        finally:
            conn.close()


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    conn = _connect()
    try:
        row = _fetch_run(conn, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return _response_from_row(row)
    finally:
        conn.close()


@app.post("/runs/{run_id}/decision")
def decide_run(run_id: str, decision: Decision) -> dict[str, Any]:
    with _write_lock:
        conn = _connect()
        try:
            row = _fetch_run(conn, run_id)
            if row is None:
                raise HTTPException(status_code=404, detail="run not found")
            response = _response_from_row(row)
            if not response.get("human_approval_required") or row["approval_status"] != "PENDING":
                return response
            status = "APPROVED" if decision.decision == "approve" else "REJECTED"
            now = _utc_now()
            conn.execute("BEGIN IMMEDIATE")
            try:
                updated = conn.execute(
                    "UPDATE runs SET approval_status = ?, updated_at = ? WHERE run_id = ? AND approval_status = 'PENDING'",
                    (status, now, run_id),
                ).rowcount
                if updated == 0:
                    conn.rollback()
                    current = _fetch_run(conn, run_id)
                    return _response_from_row(current) if current is not None else response
                conn.execute(
                    "UPDATE ledger SET status = ?, decided_at = ?, approved_by = ? WHERE run_id = ?",
                    (status, now, decision.approver, run_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return response
        finally:
            conn.close()


@app.get("/ledger")
def get_ledger() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT run_id, ticket_id, action, amount_usd, status, recorded_at FROM ledger ORDER BY entry_id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


init_db()
_ensure_prepared()
