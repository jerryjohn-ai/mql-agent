"""Typed contracts: the graph state and the model's structured output."""
from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class Action(str, Enum):
    ROUTE = "route_to_sdr"
    NURTURE = "nurture"
    DISQUALIFY = "disqualify"


class Claim(BaseModel):
    statement: str = Field(max_length=300)
    source_tool_call_id: str = Field(description="call_id of the tool result this claim comes from, or 'form'")
    source_field: str = Field(description="exact field name in that tool result, e.g. 'title'")


class ScoreResult(BaseModel):
    """What submit_score must contain. Validated before the graph moves on."""

    mql_score: int = Field(ge=0, le=100)
    confidence_score: float = Field(ge=0, le=1)
    recommended_action: Action
    missing_fields: list[str] = Field(default_factory=list)
    sdr_rationale: list[Claim] = Field(min_length=1, max_length=6)


class ScreenResult(BaseModel):
    verdict: Literal["clean", "suspicious", "gibberish"]
    reasons: list[str] = Field(default_factory=list, max_length=5)


class HumanDecision(BaseModel):
    action: Literal["approve", "adjust", "disqualify"]
    approver_id: str
    adjusted_score: int | None = Field(default=None, ge=0, le=100)
    note: str | None = Field(default=None, max_length=500)


Route = Literal["auto_route", "hitl", "auto_disqualify", "spam"]


class LeadState(BaseModel):
    idempotency_key: str
    audit_id: str
    rubric_version: str
    prompt_version: str
    policy_version: str
    lead: dict  # sanitised payload from pre-flight

    screen: ScreenResult | None = None
    tool_log: Annotated[list[dict], operator.add] = Field(default_factory=list)
    score: ScoreResult | None = None
    output_valid: bool = True
    validation_errors: list[str] = Field(default_factory=list)
    completeness: float | None = None
    blended_confidence: float | None = None
    open_opp_conflict: bool = False

    route: Route | None = None
    route_reason: str | None = None
    human_decision: HumanDecision | None = None
    final_action: str | None = None
    final_score: int | None = None
    write_back_result: dict | None = None

    usage: Annotated[list[dict], operator.add] = Field(default_factory=list)
    started_at_ms: int = 0
