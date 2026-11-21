# MQL agent: Trailhead prototype

Claude scores inbound leads. Deterministic code makes every compliance and
routing decision. Salesforce is reached only through two MCP servers:
**Server A** (read-only context) and **Server B** (writes four AI staging
fields, only with a router-signed token). Claude is never given a write tool.

This is the runnable version of the *Enterprise MQL Agent: Architecture & Build
Plan* doc (phases 1 to 4). It runs on your laptop against your Trailhead
playground, using SQLite in place of Postgres and a command-line review queue
in place of Slack.

```
lead -> pre-flight (consent, spam, sanitise, injection heuristics, idempotency)
     -> Haiku screen -> context fetch (Server A) -> Sonnet scoring (Server A tools)
     -> citation + completeness checks -> policy router
         -> auto-route --------------------> Server B writes AI_*__c fields
         -> human review (pause) -> decide -> Server B, or nurture
         -> auto-disqualify ---------------> nurture
     -> append-only audit (every input, tool call, score, decision)
```

## 1. Install (5 minutes)

You need Python 3.11+, the [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli)
(`sf`) and an Anthropic API key.

```bash
cd mql-agent
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q                            # 55 tests, no network or API key needed
```

## 2. Prepare your Trailhead playground (10 minutes)

**Log the CLI in.** If you don't know your playground password, open the
playground from Trailhead, launch the *Playground Starter* app and use *Get
Your Login Credentials* (or reset the password). Then:

```bash
sf org login web --alias mql-playground
```

**Deploy the four AI fields and two permission sets:**

```bash
cd salesforce
sf project deploy start --source-dir force-app --target-org mql-playground
sf org assign permset --name MQL_Agent_Read  --target-org mql-playground
sf org assign permset --name MQL_Agent_Write --target-org mql-playground
cd ..
```

This adds `AI_MQL_Score__c`, `AI_Confidence__c`, `AI_Rationale__c` and
`AI_Audit_ID__c` (unique external ID) to Lead. Add them to the Lead page
layout (Setup > Object Manager > Lead > Page Layouts) so you can see what the
agent writes.

> In the playground both servers log in as you (a System Administrator), so
> the permission sets add nothing yet. Server B's own field allowlist and the
> approval token still apply. For a real separation, create two integration
> users, give each one only its permission set, and point `SFA_*` and `SFB_*`
> at them.

## 3. Configure

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # paste as MQL_APPROVAL_KEY
```

Set `ANTHROPIC_API_KEY`, leave `SFA_ORG_ALIAS` / `SFB_ORG_ALIAS` as
`mql-playground`, and set `MQL_APPROVERS` to your approver id (e.g. `jerry`).

## 4. Run it

Start the two servers, each in its own terminal (venv active):

```bash
python -m servers.server_a     # read-only,  http://127.0.0.1:8101/mcp
python -m servers.server_b     # write-back, http://127.0.0.1:8102/mcp
```

**Wiring check first (no tokens spent).** This runs the full graph with a
scripted model against your real org:

```bash
MQL_LLM_PROVIDER=fake mql run --sf-lead 00QQy00000rxlu2MAA --rerun-tag wiring
```

On Windows PowerShell: `$env:MQL_LLM_PROVIDER="fake"; mql run ...`. It should
end in `pending_review`, which proves Salesforce reads work.

**Now with Claude:**

```bash
mql run --sf-recent 5                    # five newest unconverted leads
mql run --sf-lead 00QQy00000rxlu2MAA     # one lead (Jackson Controls)
mql reviews                              # what is waiting for you, with cited reasons
mql decide <audit_id> approve --as jerry
mql decide <audit_id> adjust --score 85 --note "met them at a trade show" --as jerry
mql decide <audit_id> disqualify --as jerry
mql audit                                # recent runs, routes, token use
mql show <audit_id>                      # one run with every tool call and response
```

Approved and auto-routed leads get the four AI fields written in Salesforce.
Open the Lead to see the score and the cited rationale.

Re-running a lead is a no-op (idempotency). To score it again after changing
the rubric or prompt, add `--rerun-tag v2`.

**Try to break it.** The sample form submissions include prompt injection,
zero-width tricks, a honeypot bot, missing consent and a strategic account:

```bash
mql run --file samples/leads/*.json
```

None of the hostile ones should auto-route. Form leads have no Salesforce Lead
Id, so they never write to Salesforce. Their outcome shows in `mql audit`.

## 5. What to expect from the sample data

- **Most leads go to human review at first.** The sample Leads have almost no
  firmographics, so data completeness caps the blended confidence below 0.85.
  That is the design working: thin data means a human decides.
- **The enrichment cache is synthetic.** `servers/firmographics_demo.json`
  stands in for a Clearbit/ZoomInfo cache and covers 8 sample-lead domains plus
  the fictional `northwindenergy.co.uk`. Edit it to see routing change.
- **Domain matching finds real data gaps.** The Dickenson lead
  (`dickenson.com`) does not match the Dickenson plc account
  (`dickenson-consulting.com`), and Pyramid's lead (`pyramid.net`) does not
  match `pyramid.com`. Server A matches on exact domain, then exact account
  name, and never guesses.
- **`uos.com` is set as a strategic account** in `config/policy.yaml`, so
  anything from United Oil & Gas always goes to a human.

## 6. Optional: let Salesforce do the routing

The agent stops at the staging fields. To complete the loop, build a
record-triggered Flow on Lead (on update, when `AI_MQL_Score__c` is changed and
is at least 80): set Status to *Working - Contacted* and assign the Lead to an
SDR queue. RevOps then owns the routing logic, not the agent.

## Where things live

| Path | What it is |
| --- | --- |
| `config/policy.yaml` | Thresholds, strategic domains, consent rules, limits (versioned) |
| `config/rubric_v1.md` | ICP rubric the scorer follows (demo: GenWatt generators) |
| `prompts/` | Screen and scoring system prompts (versioned) |
| `mqlagent/preflight.py` | Consent, spam, sanitising, injection heuristics, idempotency |
| `mqlagent/llm.py` | Haiku screen and Sonnet tool loop, forced `submit_score` |
| `mqlagent/router.py` | Citation check, completeness, the decision table |
| `mqlagent/graph.py` | LangGraph state machine, pause/resume, write-back |
| `mqlagent/approval.py` | HMAC approval tokens (router signs, Server B verifies) |
| `mqlagent/audit.py` | Append-only SQLite audit (UPDATE/DELETE blocked by triggers) |
| `servers/server_a.py` | MCP Server A: four model-visible read tools, two ingest-only |
| `servers/server_b.py` | MCP Server B: `stage_ai_assessment`, allowlisted fields only |
| `servers/sf_repo.py` | The only Salesforce queries that exist (fixed SOQL templates) |
| `salesforce/` | Field and permission set metadata to deploy |
| `tests/` | 55 tests: router table, sanitiser, tokens, injection, e2e over MCP |

## Not in this prototype (next steps from the build plan)

- Slack approval cards (the CLI review queue stands in) and the review SLA sweeper
- Webhook receiver and queue (leads come from Salesforce or JSON files)
- Postgres and Bedrock (`MQL_LLM_PROVIDER=bedrock` exists but is untested)
- Bearer-token auth between orchestrator and servers (they bind to 127.0.0.1)
- The nightly outcome join and golden-dataset regression gate
