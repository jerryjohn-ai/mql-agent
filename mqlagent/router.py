"""Layer 4: deterministic checks and the policy decision table. No LLM here."""
from __future__ import annotations

from .models import LeadState, ScoreResult

UNKNOWN = (None, "", "unknown", "Unknown")


def _find_field(data, field: str):
    """Look for a field at the top level of a tool result, or inside list items."""
    if isinstance(data, dict):
        if field in data:
            return data[field]
        for v in data.values():
            if isinstance(v, (dict, list)):
                found = _find_field(v, field)
                if found not in UNKNOWN:
                    return found
    if isinstance(data, list):
        for item in data:
            found = _find_field(item, field)
            if found not in UNKNOWN:
                return found
    return None


def check_citations(score: ScoreResult, tool_log: list[dict]) -> list[str]:
    """Every claim must point at a real tool result and a non-unknown field in it."""
    by_id = {t["call_id"]: t for t in tool_log}
    errors = []
    for i, claim in enumerate(score.sdr_rationale):
        rec = by_id.get(claim.source_tool_call_id)
        if rec is None:
            errors.append(f"claim {i}: unknown call_id {claim.source_tool_call_id!r}")
            continue
        if _find_field(rec.get("result", {}).get("data"), claim.source_field) in UNKNOWN:
            errors.append(f"claim {i}: field {claim.source_field!r} not present in {claim.source_tool_call_id}")
    return errors


def collect_facts(lead: dict, tool_log: list[dict]) -> dict:
    """Best known value for each rubric input, from form, CRM account and enrichment."""
    facts = {
        "industry": lead.get("industry"),
        "employees": lead.get("employees"),
        "annual_revenue": lead.get("annual_revenue"),
        "country": lead.get("country"),
        "title": lead.get("title"),
    }
    for rec in tool_log:
        res = rec.get("result") or {}
        if res.get("status") != "ok":
            continue
        d = res.get("data") or {}
        if rec["tool"] == "lookup_crm_account" and d.get("account"):
            a = d["account"]
            facts["industry"] = facts["industry"] or a.get("industry")
            facts["employees"] = facts["employees"] or a.get("employees")
            facts["annual_revenue"] = facts["annual_revenue"] or a.get("annual_revenue")
            facts["country"] = facts["country"] or a.get("country")
        if rec["tool"] == "enrich_firmographics":
            facts["industry"] = facts["industry"] or d.get("industry")
            facts["employees"] = facts["employees"] or d.get("employee_count")
            facts["annual_revenue"] = facts["annual_revenue"] or d.get("annual_revenue")
            facts["country"] = facts["country"] or d.get("hq_country")
    return facts


def completeness(lead: dict, tool_log: list[dict], policy: dict) -> float:
    facts = collect_facts(lead, tool_log)
    facts["intent_signal"] = bool(lead.get("free_text")) or lead.get("lead_source") in policy["intent_lead_sources"]
    inputs = policy["completeness_inputs"]
    present = sum(1 for k in inputs if facts.get(k) not in UNKNOWN and facts.get(k) is not False)
    return round(present / len(inputs), 3)


def open_opp_conflict(tool_log: list[dict]) -> bool:
    for rec in tool_log:
        if rec["tool"] == "check_open_opportunities":
            d = (rec.get("result") or {}).get("data") or {}
            if (d.get("open_opportunity_count") or 0) > 0:
                return True
    return False


def policy_route(state: LeadState, policy: dict) -> tuple[str, str]:
    """Precedence-ordered decision table. First match wins; every lead matches one row."""
    t = policy["thresholds"]
    domain = (state.lead.get("domain") or "").lower()
    strategic = {d.lower() for d in policy.get("strategic_domains", [])}

    if not state.output_valid or state.score is None:
        return "hitl", "invalid_model_output"
    if state.screen and state.screen.verdict == "suspicious":
        return "hitl", "screen_flag"
    if state.lead.get("injection_heuristic"):
        return "hitl", "injection_heuristic"
    if domain and any(domain == s or domain.endswith("." + s) for s in strategic):
        return "hitl", "strategic_account"
    if state.open_opp_conflict:
        return "hitl", "open_opp_conflict"

    score, conf = state.score.mql_score, state.blended_confidence or 0.0
    if score >= t["auto_route_min_score"] and conf >= t["min_confidence"]:
        return "auto_route", "high_fit"
    if score <= t["auto_disqualify_max_score"] and conf >= t["min_confidence"]:
        return "auto_disqualify", "clear_non_icp"
    return "hitl", "borderline"
