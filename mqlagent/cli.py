"""Command line: run leads, review paused leads, inspect the audit trail.

  mql run --sf-lead 00Qbm00000oriFrEAI      score one Salesforce lead
  mql run --sf-recent 5                     score the 5 newest unconverted leads
  mql run --file samples/leads/hot_lead.json
  mql reviews                               list leads waiting for a human
  mql decide <audit_id> approve --as jerry
  mql decide <audit_id> adjust --score 85 --note "known buyer" --as jerry
  mql decide <audit_id> disqualify --as jerry
  mql audit [--limit 20] | mql show <audit_id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import audit
from .settings import get_settings


def _runtime():
    from .graph import make_runtime

    return make_runtime()


def _fetch_sf_leads(rt, lead_id: str | None, recent: int | None) -> list[dict]:
    """Ingest adapter: pull Leads through Server A's ingest-only tools and shape them like form submissions."""

    async def run():
        async with rt.open_read() as a:
            ids = [lead_id] if lead_id else (await a.call("list_unconverted_leads", {"limit": recent}))["data"].get("lead_ids", [])
            out = []
            for i in ids:
                res = await a.call("get_lead", {"lead_id": i})
                if res.get("status") == "ok":
                    d = res["data"]
                    d["source"] = "salesforce"
                    d["consent"] = {"has_opted_out_of_email": bool(d.get("has_opted_out_of_email"))}
                    out.append(d)
                else:
                    print(f"  could not load {i}: {res.get('reason') or res.get('status')}")
            return out

    return asyncio.run(run())


def _print_result(label: str, r: dict) -> None:
    print(f"\n== {label}")
    for k in ("status", "reason", "audit_id", "score", "blended_confidence", "route", "route_reason", "final_action", "final_score", "write_back"):
        if r.get(k) is not None:
            print(f"   {k:<18} {r[k]}")


def cmd_run(args) -> int:
    from .graph import process_lead

    rt = _runtime()
    if args.file:
        leads = [json.loads(Path(f).read_text()) for f in args.file]
    else:
        leads = _fetch_sf_leads(rt, args.sf_lead, args.sf_recent)
    for raw in leads:
        label = f"{raw.get('company')} ({raw.get('lead_id') or raw.get('email')})"
        try:
            _print_result(label, process_lead(rt, raw, salt=args.rerun_tag or ""))
        except Exception as e:  # keep going on batch runs
            print(f"\n== {label}\n   ERROR {type(e).__name__}: {e}")
    return 0


def cmd_reviews(args) -> int:
    con = audit.connect(get_settings().audit_db)
    rows = audit.pending_reviews(con)
    if not rows:
        print("No leads waiting for review.")
    for r in rows:
        s = json.loads(r["summary"])
        print(f"\n{r['audit_id']}  {s.get('company')} - {s.get('title')}  [{s.get('domain')}]")
        print(f"   score {s.get('score')}  confidence {s.get('blended_confidence')}  reason {s.get('reason')}  screen {s.get('screen')}")
        for c in s.get("rationale") or []:
            print(f"   - {c['statement']}  [{c['source_tool_call_id']}:{c['source_field']}]")
        for e in s.get("validation_errors") or []:
            print(f"   ! {e}")
    return 0


def cmd_decide(args) -> int:
    from .graph import resume_review

    decision = {"action": args.action, "approver_id": args.as_user, "adjusted_score": args.score, "note": args.note}
    if args.action == "adjust" and args.score is None:
        print("adjust needs --score")
        return 2
    _print_result(f"decision on {args.audit_id}", resume_review(_runtime(), args.audit_id, decision))
    return 0


def cmd_audit(args) -> int:
    con = audit.connect(get_settings().audit_db)
    rows = con.execute(
        "SELECT audit_id, lead_ref, route, route_reason, final_action, final_score, blended_confidence, input_tokens, output_tokens "
        "FROM ai_audit_run ORDER BY received_at_ms DESC LIMIT ?", (args.limit,)).fetchall()
    print(f"{'audit_id':<38}{'lead':<20}{'route':<16}{'reason':<22}{'action':<24}{'score':>6}{'conf':>7}{'tokens':>9}")
    for r in rows:
        print(f"{r['audit_id']:<38}{(r['lead_ref'] or '')[:19]:<20}{r['route']:<16}{r['route_reason']:<22}{(r['final_action'] or ''):<24}"
              f"{(r['final_score'] if r['final_score'] is not None else ''):>6}{(r['blended_confidence'] or 0):>7.2f}{(r['input_tokens'] or 0) + (r['output_tokens'] or 0):>9}")
    return 0


def cmd_show(args) -> int:
    con = audit.connect(get_settings().audit_db)
    run = con.execute("SELECT * FROM ai_audit_run WHERE audit_id=?", (args.audit_id,)).fetchone()
    if not run:
        print("not found")
        return 1
    print(json.dumps({k: run[k] for k in run.keys()}, indent=1))
    for t in con.execute("SELECT * FROM ai_audit_tool_call WHERE audit_id=?", (args.audit_id,)):
        print(f"\n[{t['server']}] {t['tool']} {t['request']}  ({t['latency_ms']} ms)\n  -> {t['response'][:600]}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mql", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="score leads")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--sf-lead", help="Salesforce Lead Id")
    g.add_argument("--sf-recent", type=int, help="score the N newest unconverted Salesforce leads")
    g.add_argument("--file", nargs="+", help="JSON form submission(s)")
    r.add_argument("--rerun-tag", help="new idempotency salt, to re-score a lead already processed")
    r.set_defaults(fn=cmd_run)

    sub.add_parser("reviews", help="list leads waiting for a human").set_defaults(fn=cmd_reviews)

    d = sub.add_parser("decide", help="approve, adjust or disqualify a paused lead")
    d.add_argument("audit_id")
    d.add_argument("action", choices=["approve", "adjust", "disqualify"])
    d.add_argument("--as", dest="as_user", required=True, help="your approver id (must be in MQL_APPROVERS)")
    d.add_argument("--score", type=int)
    d.add_argument("--note")
    d.set_defaults(fn=cmd_decide)

    a = sub.add_parser("audit", help="recent runs")
    a.add_argument("--limit", type=int, default=20)
    a.set_defaults(fn=cmd_audit)

    s = sub.add_parser("show", help="one run with every tool call")
    s.add_argument("audit_id")
    s.set_defaults(fn=cmd_show)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
