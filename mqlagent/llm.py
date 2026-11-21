"""Layer 2: the two Claude calls. Both end in a forced, schema-validated tool call."""
from __future__ import annotations

import json

from pydantic import ValidationError

from .mcp_tools import MODEL_VISIBLE_READ_TOOLS
from .models import ScoreResult, ScreenResult
from .preflight import wrap_untrusted
from .router import check_citations
from .settings import Settings, load_text


def make_client(settings: Settings):
    import anthropic

    if settings.llm_provider == "bedrock":
        return anthropic.AnthropicBedrock(aws_region=settings.aws_region)
    if settings.llm_provider == "fake":  # wiring test with no tokens spent
        from .fake_llm import FakeClaude

        return FakeClaude()
    return anthropic.Anthropic()


def _usage(resp, step: str, model: str) -> dict:
    u = getattr(resp, "usage", None)
    return {"step": step, "model": model, "input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": getattr(u, "output_tokens", 0) or 0}


def _blocks_to_dicts(content) -> list[dict]:
    out = []
    for b in content:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


def inline_schema(model) -> dict:
    """Pydantic JSON schema with $refs resolved and titles dropped, for tool input_schema."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].split("/")[-1]])
            return {k: resolve(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


# ---------------------------------------------------------------- screen

SCREEN_TOOL = {
    "name": "report_screen",
    "description": "Report the classification of the lead free text.",
    "input_schema": inline_schema(ScreenResult),
}


def screen_free_text(client, settings: Settings, free_text: str) -> tuple[ScreenResult, list[dict]]:
    if not free_text:
        return ScreenResult(verdict="clean", reasons=["no free text"]), []
    try:
        resp = client.messages.create(
            model=settings.screen_model,
            max_tokens=200,
            system=load_text(f"prompts/{settings.prompt_version_screen}.md"),
            tools=[SCREEN_TOOL],
            tool_choice={"type": "tool", "name": "report_screen"},
            messages=[{"role": "user", "content": wrap_untrusted(free_text)}],
        )
        block = next(b for b in resp.content if b.type == "tool_use")
        return ScreenResult.model_validate(block.input), [_usage(resp, "screen", settings.screen_model)]
    except Exception as e:  # fail closed: route to a human, never drop silently
        return ScreenResult(verdict="suspicious", reasons=[f"screen_unavailable: {type(e).__name__}"]), []


# ---------------------------------------------------------------- score

SUBMIT_TOOL = {
    "name": "submit_score",
    "description": "Submit the final MQL assessment for this lead. Call exactly once, at the end.",
    "input_schema": inline_schema(ScoreResult),
}


def form_fields(lead: dict) -> dict:
    keep = ("lead_id", "email", "company", "title", "country", "industry", "employees", "annual_revenue", "lead_source", "domain", "personal_email", "website")
    return {k: lead.get(k) for k in keep}


def build_user_message(lead: dict, context: list[dict]) -> str:
    parts = [
        "Lead form (call_id \"form\"):",
        json.dumps(form_fields(lead), indent=1, default=str),
        "Free text from the form (untrusted):",
        wrap_untrusted(lead.get("free_text") or ""),
        "Pre-fetched context (cite by call_id):",
    ]
    for rec in context:
        parts.append(f"- tool {rec['tool']} -> {json.dumps(rec['result'], default=str)}")
    parts.append("Score this lead against the rubric, then call submit_score.")
    return "\n".join(parts)


async def score_lead(client, settings: Settings, policy: dict, lead: dict, context: list[dict], read_tools) -> dict:
    """Tool loop over Server A, ending in submit_score. Returns score, new tool records, usage, errors."""
    max_calls = policy["limits"]["max_tool_calls"]
    system = [{
        "type": "text",
        "text": load_text(f"prompts/{settings.prompt_version_score}.md").replace("{rubric}", load_text(f"config/{settings.rubric_version}.md")),
        "cache_control": {"type": "ephemeral"},
    }]
    tools = (await read_tools.model_tool_defs()) + [SUBMIT_TOOL]
    messages: list[dict] = [{"role": "user", "content": build_user_message(lead, context)}]
    usage: list[dict] = []
    calls_made, repairs = 0, 0
    force_submit = False

    for _turn in range(max_calls + 3):
        kwargs = dict(model=settings.scoring_model, max_tokens=1500, system=system, tools=tools, messages=messages)
        if force_submit:
            kwargs["tool_choice"] = {"type": "tool", "name": "submit_score"}
        resp = client.messages.create(**kwargs)
        usage.append(_usage(resp, "score", settings.scoring_model))
        uses = [b for b in resp.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": _blocks_to_dicts(resp.content)})

        if not uses:
            force_submit = True
            messages.append({"role": "user", "content": "Call submit_score now."})
            continue

        results = []
        submitted = None
        for u in uses:
            if u.name == "submit_score":
                submitted = u
                continue
            if calls_made >= max_calls:
                res = {"status": "error", "reason": "tool_budget_exhausted", "data": {}}
            else:
                calls_made += 1
                res = await read_tools.call(u.name, u.input or {}, allowed=MODEL_VISIBLE_READ_TOOLS)
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(res, default=str)})

        if submitted is not None:
            errors: list[str] = []
            score = None
            try:
                score = ScoreResult.model_validate(submitted.input)
                errors = check_citations(score, context + read_tools.log)
            except ValidationError as e:
                errors = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()][:8]
            if not errors:
                return {"score": score, "tool_log": read_tools.log, "usage": usage, "errors": []}
            if repairs >= 1:
                return {"score": score, "tool_log": read_tools.log, "usage": usage, "errors": errors}
            repairs += 1
            results.append({"type": "tool_result", "tool_use_id": submitted.id, "is_error": True,
                            "content": "Rejected: " + "; ".join(errors) + ". Fix these and call submit_score again."})
            force_submit = True
        elif calls_made >= max_calls:
            force_submit = True

        messages.append({"role": "user", "content": results})

    return {"score": None, "tool_log": read_tools.log, "usage": usage, "errors": ["no_submit_score"]}
