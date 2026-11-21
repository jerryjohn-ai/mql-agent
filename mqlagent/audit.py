"""Layer 5: append-only audit store (SQLite locally; same schema maps to Postgres).

UPDATE and DELETE on audit tables are blocked by triggers. The review queue is
operational state, not audit, so it is mutable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_audit_run (
  audit_id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE NOT NULL,
  received_at_ms INTEGER NOT NULL,
  lead_ref TEXT,
  sanitised_input TEXT NOT NULL,
  screen_model TEXT, scoring_model TEXT,
  prompt_version TEXT NOT NULL, rubric_version TEXT NOT NULL, policy_version TEXT NOT NULL,
  screen TEXT, score TEXT, validation_errors TEXT,
  completeness REAL, blended_confidence REAL,
  route TEXT NOT NULL, route_reason TEXT NOT NULL,
  human_decision TEXT, final_action TEXT, final_score INTEGER,
  write_back TEXT,
  input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER
);
CREATE TABLE IF NOT EXISTS ai_audit_tool_call (
  call_id TEXT PRIMARY KEY,
  audit_id TEXT NOT NULL,
  server TEXT NOT NULL, tool TEXT NOT NULL,
  request TEXT NOT NULL, response TEXT NOT NULL,
  latency_ms INTEGER, created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS preflight_rejects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email_hash TEXT, reason TEXT NOT NULL, created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (
  audit_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at_ms INTEGER NOT NULL,
  decided_at_ms INTEGER, decided_by TEXT
);
"""
APPEND_ONLY = ["ai_audit_run", "ai_audit_tool_call", "preflight_rejects"]


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for t in APPEND_ONLY:
        for op in ("UPDATE", "DELETE"):
            con.execute(
                f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
            )
    con.commit()
    return con


def _j(v):
    if v is None:
        return None
    if hasattr(v, "model_dump"):
        v = v.model_dump(mode="json")
    return json.dumps(v, default=str)


def mask_lead(lead: dict) -> dict:
    """PII masking for the audit copy: keep the domain, hash the person."""
    masked = dict(lead)
    for k in ("email", "first_name", "last_name"):
        if masked.get(k):
            masked[k] = "sha256:" + hashlib.sha256(str(masked[k]).encode()).hexdigest()[:16]
    return masked


def record_reject(con, email: str | None, reason: str) -> None:
    h = hashlib.sha256((email or "").lower().encode()).hexdigest()[:16] if email else None
    con.execute("INSERT INTO preflight_rejects(email_hash, reason, created_at_ms) VALUES (?,?,?)", (h, reason, int(time.time() * 1000)))
    con.commit()


def already_processed(con, idempotency_key: str) -> sqlite3.Row | None:
    return con.execute("SELECT audit_id, route, final_action FROM ai_audit_run WHERE idempotency_key=?", (idempotency_key,)).fetchone()


def write_run(con, state, settings) -> None:
    now = int(time.time() * 1000)
    tin = sum(u.get("input_tokens", 0) for u in state.usage)
    tout = sum(u.get("output_tokens", 0) for u in state.usage)
    con.execute(
        """INSERT INTO ai_audit_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            state.audit_id, state.idempotency_key, state.started_at_ms, state.lead.get("lead_id") or state.lead.get("domain"),
            _j(mask_lead(state.lead)), settings.screen_model, settings.scoring_model,
            state.prompt_version, state.rubric_version, state.policy_version,
            _j(state.screen), _j(state.score), _j(state.validation_errors),
            state.completeness, state.blended_confidence, state.route or "none", state.route_reason or "none",
            _j(state.human_decision), state.final_action, state.final_score, _j(state.write_back_result),
            tin, tout, now - state.started_at_ms if state.started_at_ms else None,
        ),
    )
    for rec in state.tool_log:
        con.execute(
            "INSERT OR IGNORE INTO ai_audit_tool_call VALUES (?,?,?,?,?,?,?,?)",
            (rec["call_id"], state.audit_id, rec["server"], rec["tool"], _j(rec.get("args")), _j(rec.get("result")), rec.get("latency_ms"), now),
        )
    con.commit()


def enqueue_review(con, audit_id: str, thread_id: str, summary: dict) -> None:
    con.execute(
        "INSERT OR IGNORE INTO review_queue(audit_id, thread_id, summary, created_at_ms) VALUES (?,?,?,?)",
        (audit_id, thread_id, json.dumps(summary, default=str), int(time.time() * 1000)),
    )
    con.commit()


def pending_reviews(con) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM review_queue WHERE status='pending' ORDER BY created_at_ms").fetchall()


def get_review(con, audit_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM review_queue WHERE audit_id=?", (audit_id,)).fetchone()


def close_review(con, audit_id: str, decided_by: str, status: str) -> None:
    con.execute(
        "UPDATE review_queue SET status=?, decided_at_ms=?, decided_by=? WHERE audit_id=?",
        (status, int(time.time() * 1000), decided_by, audit_id),
    )
    con.commit()
