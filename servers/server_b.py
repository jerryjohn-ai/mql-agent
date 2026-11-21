"""MCP Server B: Salesforce write-back. Never offered to the model.

Holds the write credentials (SFB_* env vars) and MQL_APPROVAL_KEY. It writes
only the four AI staging fields on Lead, only with a valid router-signed
approval token, and is idempotent on audit_id.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mqlagent.approval import verify  # noqa: E402
from servers.sf_repo import AI_FIELDS, make_repo  # noqa: E402

LEAD_ID_RE = re.compile(r"^00Q[A-Za-z0-9]{12}([A-Za-z0-9]{3})?$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TTL = int(os.environ.get("MQL_APPROVAL_TTL_SECONDS", "900"))
FIELD_ALLOWLIST = frozenset(AI_FIELDS)

_repo = None


def repo():
    global _repo
    if _repo is None:
        _repo = make_repo("SFB_")
    return _repo


mcp = FastMCP(
    "mql-salesforce-write",
    instructions="Writes AI staging fields on a Lead. Requires a router-signed approval token.",
    host=os.environ.get("MQL_WRITE_HOST", "127.0.0.1"),
    port=int(os.environ.get("MQL_WRITE_PORT", "8102")),
)


def build_update(score: int, confidence: float, rationale: str, audit_id: str) -> dict:
    fields = {
        "AI_MQL_Score__c": int(score),
        "AI_Confidence__c": round(float(confidence), 2),
        "AI_Rationale__c": rationale[:32000],
        "AI_Audit_ID__c": audit_id,
    }
    # Defence in depth behind the Salesforce Permission Set: drop anything not allowlisted.
    return {k: v for k, v in fields.items() if k in FIELD_ALLOWLIST}


@mcp.tool()
def stage_ai_assessment(lead_id: str, score: int, confidence: float, rationale: str, audit_id: str, approval_token: str) -> dict:
    """Write AI_MQL_Score__c, AI_Confidence__c, AI_Rationale__c and AI_Audit_ID__c on one Lead."""
    if not LEAD_ID_RE.match(lead_id or ""):
        return {"status": "rejected", "reason": "invalid_lead_id"}
    if not UUID_RE.match(audit_id or ""):
        return {"status": "rejected", "reason": "invalid_audit_id"}
    if not (0 <= int(score) <= 100) or not (0 <= float(confidence) <= 1):
        return {"status": "rejected", "reason": "out_of_range"}

    ok, why = verify(os.environ.get("MQL_APPROVAL_KEY", ""), approval_token, audit_id, lead_id, int(score), TTL)
    if not ok:
        print(f"[server-b] rejected write for {lead_id}: {why}")
        return {"status": "rejected", "reason": why}

    try:
        if repo().current_audit_id(lead_id) == audit_id:
            return {"status": "already_applied", "lead_id": lead_id, "audit_id": audit_id}
        update = build_update(score, confidence, rationale, audit_id)
        repo().update_lead(lead_id, update)
    except Exception as e:
        print(f"[server-b] write failed for {lead_id}: {type(e).__name__}: {e}")
        return {"status": "error", "reason": "salesforce_write_failed"}
    return {"status": "written", "lead_id": lead_id, "fields": sorted(update), "decision": why}


def main():
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
