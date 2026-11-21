"""Salesforce access for the MCP servers. Only these methods exist; nothing else can be queried.

Two implementations share one interface:
- SalesforceRepo: real org via simple-salesforce (fixed SOQL templates, bound parameters)
- FakeRepo: JSON fixture copied from the Trailhead sample data, for offline tests
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent

from dotenv import load_dotenv  # noqa: E402

load_dotenv(HERE.parent / ".env")  # servers run as their own processes

ACCOUNT_FIELDS = "Id, Name, Website, Industry, NumberOfEmployees, AnnualRevenue, BillingCountry, Type, OwnerId"
LEAD_FIELDS = (
    "Id, FirstName, LastName, Company, Email, Title, Country, Industry, NumberOfEmployees, "
    "AnnualRevenue, LeadSource, Status, Website, Description, IsConverted, CreatedDate"
)
AI_FIELDS = ("AI_MQL_Score__c", "AI_Confidence__c", "AI_Rationale__c", "AI_Audit_ID__c")


def host_of(website: str | None) -> str | None:
    if not website:
        return None
    w = website if "://" in website else "http://" + website
    h = (urlparse(w).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h or None


def domain_matches(website: str | None, domain: str) -> bool:
    h = host_of(website)
    return bool(h) and (h == domain or h.endswith("." + domain))


def shape_account(r: dict) -> dict:
    return {
        "account_id": r.get("Id"), "name": r.get("Name"), "website": r.get("Website"),
        "industry": r.get("Industry"), "employees": r.get("NumberOfEmployees"),
        "annual_revenue": r.get("AnnualRevenue"), "country": r.get("BillingCountry"),
        "account_type": r.get("Type"), "owner_id": r.get("OwnerId"),
    }


def shape_lead(r: dict) -> dict:
    return {
        "lead_id": r.get("Id"), "first_name": r.get("FirstName"), "last_name": r.get("LastName"),
        "company": r.get("Company"), "email": r.get("Email"), "title": r.get("Title"),
        "country": r.get("Country"), "industry": r.get("Industry"), "employees": r.get("NumberOfEmployees"),
        "annual_revenue": r.get("AnnualRevenue"), "lead_source": r.get("LeadSource"), "status": r.get("Status"),
        "website": r.get("Website"), "free_text": r.get("Description"),
        "has_opted_out_of_email": r.get("HasOptedOutOfEmail"), "is_converted": r.get("IsConverted"),
        "created_date": r.get("CreatedDate"),
    }


class SalesforceRepo:
    """Real Salesforce. Auth from env with a per-server prefix (SFA_ for read, SFB_ for write)."""

    def __init__(self, prefix: str):
        self.prefix = prefix
        self.sf = self._connect()

    def _env(self, k):
        return os.environ.get(f"{self.prefix}{k}") or None

    def _connect(self):
        from simple_salesforce import Salesforce

        alias = self._env("ORG_ALIAS")
        if alias:  # token from the sf CLI (refreshed by the CLI on each call)
            sf_bin = shutil.which("sf") or "sf"
            out = subprocess.run([sf_bin, "org", "display", "--target-org", alias, "--json"], capture_output=True, text=True, check=True)
            res = json.loads(out.stdout)["result"]
            token = res.get("accessToken") or ""
            if not token or token.startswith("[REDACTED"):
                # Newer sf CLI versions mask the token in `org display`.
                tok = subprocess.run([sf_bin, "org", "auth", "show-access-token", "--target-org", alias, "--json"],
                                     capture_output=True, text=True, check=True)
                token = json.loads(tok.stdout)["result"]["accessToken"]
            return Salesforce(instance_url=res["instanceUrl"], session_id=token)
        if self._env("ACCESS_TOKEN"):
            return Salesforce(instance_url=self._env("INSTANCE_URL"), session_id=self._env("ACCESS_TOKEN"))
        if self._env("USERNAME"):
            return Salesforce(
                username=self._env("USERNAME"), password=self._env("PASSWORD"),
                security_token=self._env("SECURITY_TOKEN") or "", domain=self._env("DOMAIN") or "login",
            )
        raise RuntimeError(f"No Salesforce credentials: set {self.prefix}ORG_ALIAS, {self.prefix}ACCESS_TOKEN or {self.prefix}USERNAME")

    def _query(self, soql: str) -> list[dict]:
        from simple_salesforce.exceptions import SalesforceExpiredSession

        try:
            return self.sf.query(soql)["records"]
        except SalesforceExpiredSession:
            self.sf = self._connect()
            return self.sf.query(soql)["records"]

    # ---- read methods (Server A) ----
    def find_account(self, domain: str | None, company: str | None) -> dict | None:
        from simple_salesforce import format_soql

        if domain:
            rows = self._query(format_soql(f"SELECT {ACCOUNT_FIELDS} FROM Account WHERE Website LIKE '%{{:like}}%' LIMIT 20", domain))
            exact = [r for r in rows if domain_matches(r.get("Website"), domain)]
            if exact:
                return shape_account(exact[0]) | {"match": "domain"}
        if company:
            rows = self._query(format_soql(f"SELECT {ACCOUNT_FIELDS} FROM Account WHERE Name = {{}} LIMIT 2", company))
            if len(rows) == 1:
                return shape_account(rows[0]) | {"match": "exact_name"}
        return None

    def open_opportunities(self, account_id: str) -> list[dict]:
        from simple_salesforce import format_soql

        rows = self._query(format_soql(
            "SELECT Id, Name, StageName, Amount, CloseDate, OwnerId FROM Opportunity "
            "WHERE AccountId = {} AND IsClosed = false ORDER BY CloseDate LIMIT 20", account_id))
        return [{"opportunity_id": r["Id"], "name": r.get("Name"), "stage": r.get("StageName"),
                 "amount": r.get("Amount"), "close_date": r.get("CloseDate"), "owner_id": r.get("OwnerId")} for r in rows]

    def lead_history(self, email: str, exclude_lead_id: str | None) -> list[dict]:
        from simple_salesforce import format_soql

        rows = self._query(format_soql(
            "SELECT Id, Status, LeadSource, IsConverted, CreatedDate FROM Lead WHERE Email = {} "
            "ORDER BY CreatedDate DESC LIMIT 10", email))
        return [{"lead_id": r["Id"], "status": r.get("Status"), "lead_source": r.get("LeadSource"),
                 "is_converted": r.get("IsConverted"), "created_date": r.get("CreatedDate")}
                for r in rows if r["Id"] != exclude_lead_id]

    def get_lead(self, lead_id: str) -> dict | None:
        from simple_salesforce import format_soql

        try:
            rows = self._query(format_soql(f"SELECT {LEAD_FIELDS}, HasOptedOutOfEmail FROM Lead WHERE Id = {{}}", lead_id))
        except Exception:  # field-level security may hide HasOptedOutOfEmail
            rows = self._query(format_soql(f"SELECT {LEAD_FIELDS} FROM Lead WHERE Id = {{}}", lead_id))
        return shape_lead(rows[0]) if rows else None

    def list_lead_ids(self, limit: int) -> list[str]:
        rows = self._query(f"SELECT Id FROM Lead WHERE IsConverted = false ORDER BY CreatedDate DESC LIMIT {int(limit)}")
        return [r["Id"] for r in rows]

    # ---- write methods (Server B) ----
    def current_audit_id(self, lead_id: str) -> str | None:
        from simple_salesforce import format_soql

        rows = self._query(format_soql("SELECT AI_Audit_ID__c FROM Lead WHERE Id = {}", lead_id))
        return rows[0].get("AI_Audit_ID__c") if rows else None

    def update_lead(self, lead_id: str, fields: dict) -> None:
        self.sf.Lead.update(lead_id, fields)


class FakeRepo:
    """Offline stand-in loaded from fake_org.json (a copy of the Trailhead sample data)."""

    def __init__(self, path: Path | None = None):
        self.path = path or HERE / "fake_org.json"
        self.db = json.loads(self.path.read_text())
        self.writes: list[tuple[str, dict]] = []

    def find_account(self, domain, company):
        if domain:
            for a in self.db["accounts"]:
                if domain_matches(a.get("Website"), domain):
                    return shape_account(a) | {"match": "domain"}
        if company:
            hits = [a for a in self.db["accounts"] if a["Name"] == company]
            if len(hits) == 1:
                return shape_account(hits[0]) | {"match": "exact_name"}
        return None

    def open_opportunities(self, account_id):
        return [{"opportunity_id": o["Id"], "name": o.get("Name"), "stage": o["StageName"], "amount": o.get("Amount"),
                 "close_date": o.get("CloseDate"), "owner_id": o.get("OwnerId")}
                for o in self.db["opportunities"] if o["AccountId"] == account_id and not o["IsClosed"]]

    def lead_history(self, email, exclude_lead_id):
        return [{"lead_id": l["Id"], "status": l["Status"], "lead_source": l.get("LeadSource"),
                 "is_converted": l.get("IsConverted"), "created_date": l.get("CreatedDate")}
                for l in self.db["leads"] if (l.get("Email") or "").lower() == email.lower() and l["Id"] != exclude_lead_id]

    def get_lead(self, lead_id):
        for l in self.db["leads"]:
            if l["Id"] == lead_id:
                return shape_lead(l)
        return None

    def list_lead_ids(self, limit):
        return [l["Id"] for l in self.db["leads"] if not l.get("IsConverted")][:limit]

    def current_audit_id(self, lead_id):
        for l in self.db["leads"]:
            if l["Id"] == lead_id:
                return l.get("AI_Audit_ID__c")
        return None

    def update_lead(self, lead_id, fields):
        for l in self.db["leads"]:
            if l["Id"] == lead_id:
                l.update(fields)
                self.writes.append((lead_id, fields))
                if os.environ.get("MQL_FAKE_PERSIST") == "1":
                    self.path.write_text(json.dumps(self.db, indent=1))
                return
        raise KeyError(f"Lead {lead_id} not found")


def make_repo(prefix: str):
    if os.environ.get("MQL_FAKE_SALESFORCE") == "1":
        return FakeRepo(Path(os.environ["MQL_FAKE_ORG_PATH"]) if os.environ.get("MQL_FAKE_ORG_PATH") else None)
    return SalesforceRepo(prefix)
