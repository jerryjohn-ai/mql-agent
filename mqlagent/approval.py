"""Approval tokens: the router signs, Server B verifies. Claude never holds the key."""
from __future__ import annotations

import hashlib
import hmac
import json
import time


def _payload(audit_id: str, lead_id: str, score: int, decision: str, issued_at: int) -> bytes:
    return json.dumps([audit_id, lead_id, int(score), decision, issued_at], separators=(",", ":")).encode()


def sign(key: str, audit_id: str, lead_id: str, score: int, decision: str, now: int | None = None) -> str:
    if not key:
        raise RuntimeError("MQL_APPROVAL_KEY is not set")
    issued = int(now if now is not None else time.time())
    mac = hmac.new(key.encode(), _payload(audit_id, lead_id, score, decision, issued), hashlib.sha256).hexdigest()
    return f"{issued}.{decision}.{mac}"


def verify(key: str, token: str, audit_id: str, lead_id: str, score: int, ttl_seconds: int, now: int | None = None) -> tuple[bool, str]:
    if not key:
        return False, "server_misconfigured"
    try:
        issued_s, decision, mac = token.split(".", 2)
        issued = int(issued_s)
    except (ValueError, AttributeError):
        return False, "malformed_token"
    now = int(now if now is not None else time.time())
    if now - issued > ttl_seconds or issued - now > 60:
        return False, "token_expired"
    if decision not in ("auto_route", "human_approved"):
        return False, "decision_not_writable"
    expected = hmac.new(key.encode(), _payload(audit_id, lead_id, score, decision, issued), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        return False, "bad_signature"
    return True, decision
