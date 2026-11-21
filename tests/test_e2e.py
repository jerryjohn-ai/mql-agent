"""End to end: real LangGraph, real MCP servers over HTTP, fake Salesforce org, scripted Claude."""
import asyncio
import json
from pathlib import Path

import pytest

from mqlagent import approval
from mqlagent.cli import _fetch_sf_leads
from mqlagent.graph import make_runtime, process_lead, resume_review
from mqlagent.mcp_tools import open_server
from mqlagent.fake_llm import FakeClaude

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples" / "leads"


def sample(name):
    return json.loads((SAMPLES / name).read_text())


def rt_for(settings, **fake_kw):
    return make_runtime(client=FakeClaude(**fake_kw), settings=settings, log=lambda *_: None)


def sf_lead(rt, lead_id):
    return _fetch_sf_leads(rt, lead_id, None)[0]


def lead_in_org(org_state, lead_id):
    return next(l for l in org_state()["leads"] if l["Id"] == lead_id)


# ---------------- happy paths
def test_form_lead_high_fit_auto_routes(settings):
    rt = rt_for(settings, scores={"Northwind Energy": (88, 0.9)})
    r = process_lead(rt, sample("hot_lead.json"))
    assert r["status"] == "done" and r["route"] == "auto_route" and r["blended_confidence"] == 0.9
    assert r["write_back"]["status"] == "skipped"  # form lead has no Salesforce Lead Id yet


def test_salesforce_lead_auto_route_writes_ai_fields_once(settings, org_state):
    rt = rt_for(settings, scores={"Jackson Controls": (85, 0.9)})
    raw = sf_lead(rt, "00Qbm00000oriFrEAI")
    r = process_lead(rt, raw)
    assert r["route"] == "auto_route" and r["write_back"]["status"] == "written"
    lead = lead_in_org(org_state, "00Qbm00000oriFrEAI")
    assert lead["AI_MQL_Score__c"] == 85 and lead["AI_Audit_ID__c"] == r["audit_id"]
    assert "Buyer title is SVP, Procurement [form:title]" in lead["AI_Rationale__c"]
    # Re-running the same lead is a no-op (idempotency key = lead id)
    assert process_lead(rt, raw)["status"] == "duplicate"


def test_clear_non_icp_goes_to_nurture_without_write(settings, org_state):
    rt = rt_for(settings, scores={"Delphi Chemicals": (30, 0.95)})
    r = process_lead(rt, sf_lead(rt, "00Qbm00000oriFxEAI"))
    assert r["route"] == "auto_disqualify" and r["final_action"] == "nurture"
    assert "AI_MQL_Score__c" not in lead_in_org(org_state, "00Qbm00000oriFxEAI")


# ---------------- human review: pause, survive restart, resume
def test_borderline_pauses_then_approval_after_restart_writes(settings, org_state):
    rt = rt_for(settings, scores={"Metropolitan Health Services": (82, 0.9)})
    r = process_lead(rt, sf_lead(rt, "00Qbm00000oriFsEAI"))
    # Purchased List + no free text = no intent signal -> completeness 5/6 caps confidence at 0.833
    assert r["status"] == "pending_review" and r["route_reason"] == "borderline" and r["blended_confidence"] == 0.833

    rt2 = rt_for(settings)  # new process: graph rebuilt from the checkpoint DB
    assert resume_review(rt2, r["audit_id"], {"action": "approve", "approver_id": "mallory"})["status"] == "not_an_approver"
    done = resume_review(rt2, r["audit_id"], {"action": "approve", "approver_id": "jerry"})
    assert done["status"] == "done" and done["write_back"]["status"] == "written" and done["write_back"]["decision"] == "human_approved"
    assert lead_in_org(org_state, "00Qbm00000oriFsEAI")["AI_MQL_Score__c"] == 82
    assert resume_review(rt2, r["audit_id"], {"action": "approve", "approver_id": "jerry"})["status"] == "already_decided"


def test_adjust_score_writes_adjusted_value(settings, org_state):
    rt = rt_for(settings)  # default fake score 60 / 0.7 -> borderline
    r = process_lead(rt, sf_lead(rt, "00Qbm00000oriFvEAI"))
    assert r["status"] == "pending_review"
    done = resume_review(rt, r["audit_id"], {"action": "adjust", "approver_id": "jerry", "adjusted_score": 82, "note": "known buyer"})
    assert done["final_score"] == 82
    lead = lead_in_org(org_state, "00Qbm00000oriFvEAI")
    assert lead["AI_MQL_Score__c"] == 82 and "adjust by jerry - known buyer" in lead["AI_Rationale__c"]


def test_strategic_account_always_reviewed_and_disqualify_skips_write(settings):
    rt = rt_for(settings, scores={"United Oil & Gas Corp.": (95, 0.99)})
    r = process_lead(rt, sample("strategic_uos.json"))
    assert r["status"] == "pending_review" and r["route_reason"] == "strategic_account"
    done = resume_review(rt, r["audit_id"], {"action": "disqualify", "approver_id": "jerry"})
    assert done["final_action"] == "disqualified_by_human" and done["write_back"]["status"] == "skipped"


# ---------------- hostile input
def test_injection_caught_by_screen_even_if_model_obeys(settings):
    rt = rt_for(settings, obey_injection=True)
    r = process_lead(rt, sample("injection_lead.json"))
    assert r["score"] == 100  # the fake model was fooled...
    assert r["status"] == "pending_review" and r["route_reason"] == "screen_flag"  # ...and it did not matter


def test_injection_caught_by_heuristic_when_screen_also_misses(settings):
    rt = rt_for(settings, obey_injection=True, screen_misses=True)
    r = process_lead(rt, sample("injection_lead.json"))
    assert r["score"] == 100 and r["route_reason"] == "injection_heuristic"


def test_zero_width_injection_flagged(settings):
    rt = rt_for(settings, screen_misses=True)
    r = process_lead(rt, sample("zero_width_injection.json"))
    assert r["route_reason"] == "injection_heuristic"


def test_model_never_sees_write_tool(settings):
    rt = rt_for(settings, scores={"Northwind Energy": (88, 0.9)})
    process_lead(rt, sample("hot_lead.json"), salt="tools-check")
    for call in rt.client.calls:
        names = {t["name"] for t in call.get("tools", [])}
        assert "stage_ai_assessment" not in names and "get_lead" not in names


def test_server_b_rejects_forged_and_replayed_tokens(settings):
    async def call(args):
        async with open_server(settings.write_server_url, "B") as b:
            return await b.call("stage_ai_assessment", args)

    base = {"lead_id": "00Qbm00000oriG0EAI", "score": 99, "confidence": 0.9, "rationale": "x",
            "audit_id": "11111111-2222-3333-4444-555555555555"}
    assert asyncio.run(call(base | {"approval_token": "123.auto_route.deadbeef"}))["status"] == "rejected"
    forged = approval.sign("wrong-key", base["audit_id"], base["lead_id"], 99, "auto_route")
    assert asyncio.run(call(base | {"approval_token": forged}))["reason"] == "bad_signature"
    good = approval.sign(settings.approval_key, base["audit_id"], base["lead_id"], 99, "auto_route")
    assert asyncio.run(call(base | {"score": 100, "approval_token": good}))["reason"] == "bad_signature"  # score tampered


# ---------------- pre-flight, spam, failures
@pytest.mark.parametrize("name,reason", [
    ("no_consent.json", "no_marketing_consent"), ("disposable_domain.json", "disposable_domain"), ("honeypot_bot.json", "honeypot_hit"),
])
def test_preflight_rejects_never_call_claude(settings, name, reason):
    rt = rt_for(settings)
    assert process_lead(rt, sample(name)) == {"status": "rejected_preflight", "reason": reason}
    assert rt.client.calls == []


def test_gibberish_discarded_after_screen_only(settings):
    rt = rt_for(settings)
    r = process_lead(rt, sample("gibberish.json"))
    assert r["route"] == "spam" and len(rt.client.calls) == 1


def test_bad_citation_repaired_once(settings):
    rt = rt_for(settings, scores={"Northwind Energy": (88, 0.9)}, bad_citation_once=True)
    r = process_lead(rt, sample("hot_lead.json"), salt="repair")
    assert r["route"] == "auto_route"
    assert any(isinstance(m["content"], list) and any(b.get("is_error") for b in m["content"] if isinstance(b, dict))
               for c in rt.client.calls for m in c["messages"])


def test_read_server_down_fails_safe_to_review(settings):
    from dataclasses import replace

    rt = rt_for(replace(settings, read_server_url="http://127.0.0.1:1/mcp"), scores={"Northwind Energy": (95, 1.0)})
    r = process_lead(rt, sample("hot_lead.json"), salt="down")
    assert r["status"] == "pending_review" and r["route_reason"] == "invalid_model_output"


def test_audit_trail_has_every_tool_call(settings):
    rt = rt_for(settings, scores={"Jackson Controls": (85, 0.9)})
    r = process_lead(rt, sf_lead(rt, "00Qbm00000oriFrEAI"), salt="audit")
    tools = [t["tool"] for t in rt.con.execute("SELECT tool FROM ai_audit_tool_call WHERE audit_id=?", (r["audit_id"],))]
    assert {"lead_form", "lookup_crm_account", "enrich_firmographics", "get_prior_lead_history"} <= set(tools)
    run = rt.con.execute("SELECT sanitised_input, policy_version FROM ai_audit_run WHERE audit_id=?", (r["audit_id"],)).fetchone()
    assert "jeffg@jackson.com" not in run["sanitised_input"] and run["policy_version"] == "policy-v1"
