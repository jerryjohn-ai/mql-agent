"""The LangGraph state machine: one run per lead, thread_id = idempotency key."""
from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import approval, audit
from .llm import form_fields, make_client, score_lead, screen_free_text
from .mcp_tools import open_server
from .models import Action, HumanDecision, LeadState
from .preflight import run_preflight
from .router import completeness, open_opp_conflict, policy_route
from .settings import Settings, get_settings, load_policy


@dataclass
class Runtime:
    settings: Settings
    policy: dict
    client: Any
    con: sqlite3.Connection
    open_read: Callable = None
    open_write: Callable = None
    graph: Any = None
    log: Callable[[str], None] = print

    def __post_init__(self):
        s = self.settings
        self.open_read = self.open_read or (lambda: open_server(s.read_server_url, "A", s.read_server_token))
        self.open_write = self.open_write or (lambda: open_server(s.write_server_url, "B", s.write_server_token))


def _form_record(lead: dict) -> dict:
    return {"call_id": "form", "server": "form", "tool": "lead_form", "args": {}, "latency_ms": 0,
            "result": {"call_id": "form", "status": "ok", "data": form_fields(lead)}}


def rationale_text(state: LeadState, score: int) -> str:
    lines = [f"AI MQL score {score}/100 (route: {state.route}, reason: {state.route_reason})"]
    if state.score:
        lines += [f"- {c.statement} [{c.source_tool_call_id}:{c.source_field}]" for c in state.score.sdr_rationale]
        if state.score.missing_fields:
            lines.append("Missing: " + ", ".join(state.score.missing_fields))
    if state.human_decision:
        h = state.human_decision
        lines.append(f"Human decision: {h.action} by {h.approver_id}" + (f" - {h.note}" if h.note else ""))
    lines.append(f"Audit ID: {state.audit_id}")
    return "\n".join(lines)


def build_graph(rt: Runtime):
    s, policy = rt.settings, rt.policy

    def screen(state: LeadState):
        result, usage = screen_free_text(rt.client, s, state.lead.get("free_text") or "")
        return {"screen": result, "usage": usage}

    def after_screen(state: LeadState):
        return "spam" if state.screen and state.screen.verdict == "gibberish" else "gather_context"

    def mark_spam(state: LeadState):
        return {"route": "spam", "route_reason": "gibberish_free_text", "final_action": "discarded"}

    def gather_context(state: LeadState):
        """Deterministic context fetch: the router depends on it, so it never depends on the model's choices."""
        lead = state.lead

        async def run():
            async with rt.open_read() as tools:
                acct = await tools.call("lookup_crm_account", {"domain": lead.get("domain") or "", "company_name": lead.get("company") or ""})
                account = (acct.get("data") or {}).get("account")
                if account:
                    await tools.call("check_open_opportunities", {"account_id": account["account_id"]})
                if lead.get("domain"):
                    await tools.call("enrich_firmographics", {"domain": lead["domain"]})
                return tools.log

        try:
            records = asyncio.run(run())
        except Exception as e:
            rt.log(f"  ! Server A unavailable during context fetch: {type(e).__name__}")
            records = []
        return {"tool_log": [_form_record(lead)] + records}

    def score(state: LeadState):
        async def run():
            async with rt.open_read() as tools:
                return await score_lead(rt.client, s, policy, state.lead, state.tool_log, tools)

        try:
            out = asyncio.run(run())
        except Exception as e:
            rt.log(f"  ! scoring failed: {type(e).__name__}: {e}")
            out = {"score": None, "tool_log": [], "usage": [], "errors": [f"scoring_failed: {type(e).__name__}"]}
        return {"score": out["score"], "tool_log": out["tool_log"], "usage": out["usage"],
                "output_valid": out["score"] is not None and not out["errors"], "validation_errors": out["errors"]}

    def evaluate(state: LeadState):
        comp = completeness(state.lead, state.tool_log, policy)
        blended = round(min(state.score.confidence_score, comp), 3) if state.score else 0.0
        return {"completeness": comp, "blended_confidence": blended, "open_opp_conflict": open_opp_conflict(state.tool_log)}

    def route(state: LeadState):
        r, reason = policy_route(state, policy)
        return {"route": r, "route_reason": reason}

    def human_review(state: LeadState):
        audit.enqueue_review(rt.con, state.audit_id, state.idempotency_key, {
            "company": state.lead.get("company"), "title": state.lead.get("title"), "domain": state.lead.get("domain"),
            "lead_id": state.lead.get("lead_id"), "score": state.score.mql_score if state.score else None,
            "recommended": state.score.recommended_action.value if state.score else None,
            "blended_confidence": state.blended_confidence, "reason": state.route_reason,
            "screen": state.screen.verdict if state.screen else None,
            "rationale": [c.model_dump() for c in state.score.sdr_rationale] if state.score else [],
            "validation_errors": state.validation_errors,
        })
        decision = interrupt({"audit_id": state.audit_id, "reason": state.route_reason})
        return {"human_decision": HumanDecision.model_validate(decision)}

    def after_review(state: LeadState):
        return "nurture" if state.human_decision.action == "disqualify" else "write_back"

    def after_route(state: LeadState):
        return {"auto_route": "write_back", "hitl": "human_review", "auto_disqualify": "nurture"}[state.route]

    def write_back(state: LeadState):
        h = state.human_decision
        final_score = h.adjusted_score if (h and h.action == "adjust" and h.adjusted_score is not None) else state.score.mql_score
        conf = state.blended_confidence or 0.0
        decision = "human_approved" if h else "auto_route"
        lead_id = state.lead.get("lead_id")
        if not lead_id:
            result = {"status": "skipped", "reason": "no_salesforce_lead_id"}
        else:
            token = approval.sign(s.approval_key, state.audit_id, lead_id, final_score, decision)

            async def run():
                async with rt.open_write() as b:
                    return await b.call("stage_ai_assessment", {
                        "lead_id": lead_id, "score": final_score, "confidence": conf,
                        "rationale": rationale_text(state, final_score), "audit_id": state.audit_id, "approval_token": token,
                    })

            try:
                result = asyncio.run(run())
            except Exception as e:
                result = {"status": "error", "reason": f"server_b_unavailable: {type(e).__name__}"}
        return {"final_action": "routed_to_sdr", "final_score": final_score, "write_back_result": result}

    def nurture(state: LeadState):
        h = state.human_decision
        action = "disqualified_by_human" if h else Action.NURTURE.value
        return {"final_action": action, "final_score": state.score.mql_score if state.score else None,
                "write_back_result": {"status": "skipped", "reason": "nurture_path"}}

    def write_audit(state: LeadState):
        audit.write_run(rt.con, state, s)
        if state.human_decision:
            audit.close_review(rt.con, state.audit_id, state.human_decision.approver_id, "decided")
        return {}

    g = StateGraph(LeadState)
    for name, fn in [("screen", screen), ("mark_spam", mark_spam), ("gather_context", gather_context), ("score", score),
                     ("evaluate", evaluate), ("route", route), ("human_review", human_review),
                     ("write_back", write_back), ("nurture", nurture), ("audit", write_audit)]:
        g.add_node(name, fn)
    g.add_edge(START, "screen")
    g.add_conditional_edges("screen", after_screen, {"spam": "mark_spam", "gather_context": "gather_context"})
    g.add_edge("mark_spam", "audit")
    g.add_edge("gather_context", "score")
    g.add_edge("score", "evaluate")
    g.add_edge("evaluate", "route")
    g.add_conditional_edges("route", after_route, {"write_back": "write_back", "human_review": "human_review", "nurture": "nurture"})
    g.add_conditional_edges("human_review", after_review, {"write_back": "write_back", "nurture": "nurture"})
    g.add_edge("write_back", "audit")
    g.add_edge("nurture", "audit")
    g.add_edge("audit", END)

    # Explicitly allow only our own state types to be restored from checkpoints.
    serde = JsonPlusSerializer(allowed_msgpack_modules=[
        ("mqlagent.models", n) for n in ("ScreenResult", "ScoreResult", "Claim", "Action", "HumanDecision", "LeadState")])
    saver = SqliteSaver(sqlite3.connect(s.checkpoint_db, check_same_thread=False), serde=serde)
    return g.compile(checkpointer=saver)


def make_runtime(client=None, open_read=None, open_write=None, settings: Settings | None = None, policy: dict | None = None, log=print) -> Runtime:
    s = settings or get_settings()
    rt = Runtime(settings=s, policy=policy or load_policy(), client=client if client is not None else make_client(s),
                 con=audit.connect(s.audit_db), open_read=open_read, open_write=open_write, log=log)
    rt.graph = build_graph(rt)
    return rt


def process_lead(rt: Runtime, raw: dict, salt: str = "") -> dict:
    """Pre-flight, idempotency check, then one graph run. Returns a short summary."""
    pf = run_preflight(raw, rt.policy, salt)
    if not pf.accepted:
        audit.record_reject(rt.con, raw.get("email"), pf.reason)
        return {"status": "rejected_preflight", "reason": pf.reason}

    done = audit.already_processed(rt.con, pf.idempotency_key)
    if done:
        return {"status": "duplicate", "audit_id": done["audit_id"], "route": done["route"], "final_action": done["final_action"]}

    config = {"configurable": {"thread_id": pf.idempotency_key}}
    snap = rt.graph.get_state(config)
    if snap and snap.next:
        return {"status": "already_pending_review", "audit_id": snap.values.get("audit_id")}

    state = LeadState(
        idempotency_key=pf.idempotency_key, audit_id=str(uuid.uuid4()), rubric_version=rt.settings.rubric_version,
        prompt_version=f"{rt.settings.prompt_version_screen}+{rt.settings.prompt_version_score}",
        policy_version=rt.policy["version"], lead=pf.lead, started_at_ms=int(time.time() * 1000),
    )
    out = rt.graph.invoke(state, config)
    return summarise(out)


def resume_review(rt: Runtime, audit_id: str, decision: dict) -> dict:
    row = audit.get_review(rt.con, audit_id)
    if row is None:
        return {"status": "not_found"}
    if row["status"] != "pending":
        return {"status": "already_decided"}
    if rt.settings.approvers and decision.get("approver_id") not in rt.settings.approvers:
        return {"status": "not_an_approver"}
    HumanDecision.model_validate(decision)
    out = rt.graph.invoke(Command(resume=decision), {"configurable": {"thread_id": row["thread_id"]}})
    return summarise(out)


def summarise(out: dict) -> dict:
    if "__interrupt__" in out:
        return {"status": "pending_review", "audit_id": out.get("audit_id"), "route_reason": out.get("route_reason"),
                "score": out["score"].mql_score if out.get("score") else None, "blended_confidence": out.get("blended_confidence")}
    return {
        "status": "done", "audit_id": out.get("audit_id"), "route": out.get("route"), "route_reason": out.get("route_reason"),
        "score": out["score"].mql_score if out.get("score") else None, "final_score": out.get("final_score"),
        "blended_confidence": out.get("blended_confidence"), "final_action": out.get("final_action"),
        "write_back": out.get("write_back_result"),
    }
