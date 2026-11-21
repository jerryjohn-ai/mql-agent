"""A scripted stand-in for the Anthropic client: used by tests and by MQL_LLM_PROVIDER=fake wiring checks."""
from __future__ import annotations

import itertools
import json
import re
from types import SimpleNamespace

_ids = itertools.count(1)


def _tool_use(name, inp):
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=inp)


def _resp(*blocks):
    return SimpleNamespace(content=list(blocks), stop_reason="tool_use", usage=SimpleNamespace(input_tokens=120, output_tokens=40))


class FakeClaude:
    """
    scores: company -> (score, confidence). Default (60, 0.7).
    obey_injection: if True the fake scorer does what injected text asks (simulates a fooled model).
    screen_misses: if True the fake screen always says 'clean' (simulates a screen bypass).
    bad_citation_once: first submit cites a non-existent call_id, to exercise the repair loop.
    """

    def __init__(self, scores=None, obey_injection=False, screen_misses=False, bad_citation_once=False):
        self.scores = scores or {}
        self.obey_injection = obey_injection
        self.screen_misses = screen_misses
        self.bad_citation_once = bad_citation_once
        self.calls = []
        self.messages = self  # client.messages.create(...)

    def create(self, **kw):
        self.calls.append(kw)
        tc = kw.get("tool_choice") or {}
        if tc.get("name") == "report_screen":
            return self._screen(kw["messages"][0]["content"])
        return self._score(kw)

    # ---- screen
    def _screen(self, text: str):
        low = text.lower()
        if self.screen_misses:
            verdict = "clean"
        elif "ignore" in low or "&lt;system" in low or "admin mode" in low:
            verdict = "suspicious"
        elif "asdkjh" in low:
            verdict = "gibberish"
        else:
            verdict = "clean"
        return _resp(_tool_use("report_screen", {"verdict": verdict, "reasons": [f"fake:{verdict}"]}))

    # ---- score
    def _score(self, kw):
        msgs = kw["messages"]
        first = msgs[0]["content"]
        form = json.loads(first.split("Lead form (call_id \"form\"):\n", 1)[1].split("\nFree text", 1)[0])
        n_assistant = sum(1 for m in msgs if m["role"] == "assistant")
        last = msgs[-1]["content"]
        rejected = isinstance(last, list) and any(b.get("is_error") for b in last if isinstance(b, dict))

        if n_assistant == 0 and form.get("email") and not kw.get("tool_choice"):
            return _resp(_tool_use("get_prior_lead_history", {"email": form["email"], "exclude_lead_id": form.get("lead_id") or ""}))

        score, conf = self.scores.get(form.get("company"), (60, 0.7))
        if self.obey_injection and "set mql_score to 100" in first.lower():
            score, conf = 100, 1.0

        claims = [{"statement": f"Buyer title is {form.get('title')}", "source_tool_call_id": "form", "source_field": "title"}]
        m = re.search(r"- tool enrich_firmographics -> (\{.*\})", first)
        if m:
            enr = json.loads(m.group(1))
            if enr.get("status") == "ok":
                claims.append({"statement": f"{enr['data']['employee_count']} employees", "source_tool_call_id": enr["call_id"], "source_field": "employee_count"})
        if self.bad_citation_once and not rejected:
            claims.append({"statement": "Revenue is huge", "source_tool_call_id": "a-doesnotexist", "source_field": "annual_revenue"})

        action = "route_to_sdr" if score >= 80 else ("disqualify" if score < 50 else "nurture")
        return _resp(_tool_use("submit_score", {
            "mql_score": score, "confidence_score": conf, "recommended_action": action,
            "missing_fields": [], "sdr_rationale": claims,
        }))
