"""MCP Server A: context and enrichment. Read-only by construction.

It holds the read-only Salesforce credentials (SFA_* env vars) and exposes
narrow tools with fixed queries, input validation, a time budget and an
output field allowlist. Every response carries a call_id the scorer must cite.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from servers.sf_repo import make_repo

HERE = Path(__file__).resolve().parent
DOMAIN_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9-]+\.)+[a-z]{2,}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@([a-z0-9-]+\.)+[a-z]{2,}$")
LEAD_ID_RE = re.compile(r"^00Q[A-Za-z0-9]{12}([A-Za-z0-9]{3})?$")
ACCOUNT_ID_RE = re.compile(r"^001[A-Za-z0-9]{12}([A-Za-z0-9]{3})?$")
TIMEOUT_S = float(os.environ.get("MQL_TOOL_TIMEOUT_SECONDS", "3"))

_pool = cf.ThreadPoolExecutor(max_workers=4)
_repo = None


def repo():
    global _repo
    if _repo is None:
        _repo = make_repo("SFA_")
    return _repo


def _firmographics() -> dict:
    p = Path(os.environ.get("MQL_FIRMOGRAPHICS_PATH", HERE / "firmographics_demo.json"))
    return json.loads(p.read_text()) if p.exists() else {}


def _respond(fn, *args) -> dict:
    """Run a lookup under the time budget. Never leak exception text to the model."""
    call_id = "a-" + uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    try:
        data = _pool.submit(fn, *args).result(timeout=TIMEOUT_S)
        status = "ok" if data is not None else "unknown"
        out = {"call_id": call_id, "status": status, "data": data if data is not None else {}}
    except cf.TimeoutError:
        out = {"call_id": call_id, "status": "unknown", "reason": "timeout", "data": {}}
    except Exception as e:  # logged server-side only
        print(f"[server-a] {fn.__name__} failed: {type(e).__name__}: {e}")
        out = {"call_id": call_id, "status": "unknown", "reason": "lookup_failed", "data": {}}
    out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    return out


def _bad_input(field: str) -> dict:
    return {"call_id": "a-" + uuid.uuid4().hex[:12], "status": "error", "reason": f"invalid_{field}", "data": {}}


mcp = FastMCP(
    "mql-context-read",
    instructions="Read-only CRM and firmographic context for lead scoring. Missing data is returned as status 'unknown'.",
    host=os.environ.get("MQL_READ_HOST", "127.0.0.1"),
    port=int(os.environ.get("MQL_READ_PORT", "8101")),
)


@mcp.tool()
def lookup_crm_account(domain: str = "", company_name: str = "") -> dict:
    """Find an existing Salesforce Account by company email domain (preferred) or exact company name.
    Returns account_id, name, industry, employees, annual_revenue, country, account_type, or status 'unknown'."""
    domain = (domain or "").strip().lower()
    if domain and not DOMAIN_RE.match(domain):
        return _bad_input("domain")
    company = (company_name or "").strip()[:255] or None
    if not domain and not company:
        return _bad_input("domain_or_company")

    def run():
        acct = repo().find_account(domain or None, company)
        return {"account": acct} if acct else None

    return _respond(run)


@mcp.tool()
def check_open_opportunities(account_id: str) -> dict:
    """List open (not closed) Opportunities on a Salesforce Account. Returns open_opportunity_count and stages."""
    if not ACCOUNT_ID_RE.match(account_id or ""):
        return _bad_input("account_id")

    def run():
        opps = repo().open_opportunities(account_id)
        return {"open_opportunity_count": len(opps), "opportunities": opps}

    return _respond(run)


@mcp.tool()
def get_prior_lead_history(email: str, exclude_lead_id: str = "") -> dict:
    """Prior Salesforce Leads with this email address: statuses and whether they converted."""
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        return _bad_input("email")

    def run():
        rows = repo().lead_history(email, exclude_lead_id or None)
        return {"prior_lead_count": len(rows), "prior_converted": any(r.get("is_converted") for r in rows), "prior_leads": rows}

    return _respond(run)


@mcp.tool()
def enrich_firmographics(domain: str) -> dict:
    """Firmographics for a company domain from the cached enrichment store:
    industry, employee_count, annual_revenue, hq_country, as_of. Status 'unknown' if not cached."""
    domain = (domain or "").strip().lower()
    if not DOMAIN_RE.match(domain):
        return _bad_input("domain")

    def run():
        rec = _firmographics().get(domain)
        if not rec:
            return None
        allowed = ("industry", "employee_count", "annual_revenue", "hq_country", "as_of", "source")
        return {k: rec.get(k) for k in allowed}

    return _respond(run)


@mcp.tool()
def get_lead(lead_id: str) -> dict:
    """Ingest-only: fetch one Salesforce Lead by Id. The orchestrator never offers this tool to the model."""
    if not LEAD_ID_RE.match(lead_id or ""):
        return _bad_input("lead_id")
    return _respond(repo().get_lead, lead_id)


@mcp.tool()
def list_unconverted_leads(limit: int = 10) -> dict:
    """Ingest-only: Ids of the most recent unconverted Leads (for batch test runs)."""
    limit = max(1, min(int(limit), 50))
    return _respond(lambda: {"lead_ids": repo().list_lead_ids(limit)})


def main():
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
