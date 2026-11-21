"""Layer 1: deterministic pre-flight. No LLM is called for a lead that fails here."""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")
FREE_MAIL = {"gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com", "live.com", "aol.com", "proton.me", "protonmail.com"}
# Unicode categories removed from free text: control (Cc) except \n\t, format (Cf: zero-width, bidi overrides)
_KEEP_CC = {"\n", "\t"}

# Deterministic injection heuristics, checked after sanitisation. A hit forces human review
# (it does not drop the lead), so false positives cost a reviewer a click, not a lost lead.
INJECTION_PATTERNS = [
    re.compile(r"\bignore\b.{0,40}\b(instructions?|rules?|rubric|prompt|above|previous)\b", re.I | re.S),
    re.compile(r"\b(mql_score|confidence_score|recommended_action|submit_score|stage_ai_assessment|approval_token)\b", re.I),
    re.compile(r"\b(system|developer|admin)\s+(prompt|mode|message|override)\b", re.I),
    re.compile(r"&lt;/?\s*(system|lead_text|tool|assistant|user|instructions?)\b", re.I),
    re.compile(r"\byou are now\b", re.I),
]

STRING_FIELDS = ("first_name", "last_name", "company", "title", "country", "industry", "lead_source", "website")


@dataclass
class PreflightResult:
    accepted: bool
    reason: str
    lead: dict | None = None
    idempotency_key: str | None = None


def sanitise_text(text: str | None, max_chars: int) -> str:
    """Normalise and neutralise untrusted free text before any model sees it."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = "".join(ch for ch in t if ch in _KEEP_CC or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs"))
    # Escape angle brackets so nothing inside can close or open our delimiter tags.
    t = html.escape(t, quote=False)
    t = re.sub(r"[ \t]{3,}", "  ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > max_chars:
        t = t[:max_chars] + " [truncated]"
    return t


def injection_hits(text: str) -> list[str]:
    return [p.pattern[:40] for p in INJECTION_PATTERNS if p.search(text or "")]


def wrap_untrusted(text: str) -> str:
    return f"<lead_text>\n{text}\n</lead_text>" if text else "<lead_text></lead_text>"


def _clean_field(v, max_len: int = 255):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = sanitise_text(str(v), max_len)
    return s or None


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower()


def idempotency_key(lead: dict, salt: str = "") -> str:
    if lead.get("source") == "salesforce" and lead.get("lead_id"):
        basis = f"sf|{lead['lead_id']}|{salt}"
    else:
        # Same email + form + hour bucket = same submission (absorbs webhook retries).
        bucket = (lead.get("submitted_at") or datetime.now(timezone.utc).isoformat())[:13]
        basis = f"form|{(lead.get('email') or '').strip().lower()}|{lead.get('form_id', 'default')}|{bucket}|{salt}"
    return hashlib.sha256(basis.encode()).hexdigest()


def run_preflight(raw: dict, policy: dict, salt: str = "") -> PreflightResult:
    limits, spam, consent = policy["limits"], policy["spam"], policy["consent"]

    email = (raw.get("email") or "").strip()
    if not EMAIL_RE.match(email):
        return PreflightResult(False, "invalid_email")
    domain = email_domain(email)

    # Spam / bot gates
    if raw.get(spam["honeypot_field"]):
        return PreflightResult(False, "honeypot_hit")
    if domain in set(spam["disposable_domains"]):
        return PreflightResult(False, "disposable_domain")

    # Consent gate
    source = raw.get("source", "form")
    c = raw.get("consent") or {}
    if source == "form" and consent["require_explicit_for_form_leads"] and c.get("marketing") is not True:
        return PreflightResult(False, "no_marketing_consent")
    if source == "salesforce" and (c.get("has_opted_out_of_email") or not consent["crm_leads_assume_basis"]):
        return PreflightResult(False, "crm_opted_out")

    lead = {k: _clean_field(raw.get(k)) for k in STRING_FIELDS}
    lead.update(
        source=source,
        lead_id=raw.get("lead_id"),
        email=email.lower(),
        domain=None if domain in FREE_MAIL else domain,
        personal_email=domain in FREE_MAIL,
        employees=raw.get("employees") if isinstance(raw.get("employees"), (int, float)) else None,
        annual_revenue=raw.get("annual_revenue") if isinstance(raw.get("annual_revenue"), (int, float)) else None,
        free_text=sanitise_text(raw.get("free_text"), limits["max_free_text_chars"]),
        form_id=raw.get("form_id"),
        submitted_at=raw.get("submitted_at"),
    )
    lead["injection_heuristic"] = injection_hits(lead["free_text"])
    return PreflightResult(True, "accepted", lead, idempotency_key(lead, salt))
