# Project 11 — n8n Business Automation Pipeline

**Client:** TCS · **Industry:** Enterprise IT · **Ref:** PX-N8N-2026-011

An enterprise-grade, AI-assisted automation pipeline that takes an IT service
request from intake to completion without manual routing: validate → enrich →
classify with an LLM → apply business rules → route → write to the system of
record → obtain approval where required → notify → log and evaluate.

> All employee names, email addresses, asset tags, queues and cost figures in
> this repository are **synthetic**. No production or client data is used.

---

## What it does, in one pass

```
POST /webhook/it-request
  │
  ├─ Normalize        mint trace_id + ticket_id, flatten the payload
  ├─ Validate         required fields, formats, cost ceiling, injection screen
  │                     └─ invalid → 400 + explanatory email + WARN audit row
  ├─ Enrich (×3)      employee directory · assigned assets · knowledge base
  ├─ AI Triage        OpenAI, strict JSON: category, priority, sentiment, entities
  ├─ Business Rules   12 deterministic rules that can override the model
  ├─ Route            Switch → 5 specialist queues + Human Review fallback
  ├─ Create Ticket    authenticated write to the system of record (3 retries)
  │                     └─ failure → ERROR row + page IT Ops + 502 to caller
  ├─ Approval?        cost ≥ threshold or privileged access
  │                     └─ sub-workflow: email with Approve/Reject links,
  │                        Wait up to 48h, escalate on timeout
  ├─ Persist          app.tickets + app.audit_log
  ├─ Notify           requester confirmation + queue assignment
  └─ Respond          full decision trace as JSON
```

Two more workflows run alongside it: a **15-minute SLA sweep** that escalates
overdue tickets, and a **global error handler** wired as `errorWorkflow` on
everything else.

---

## Requirement coverage

| # | Requirement | Where it lives |
|---|---|---|
| 4.1 | Workflow trigger | `Webhook: IT Request` (main) · `Schedule: Every 15 Minutes` (SLA sweep) · `executeWorkflowTrigger` (approval) · `errorTrigger` (handler) |
| 4.2 | Input validation | `Validate Request` — field rules, formats, cost ceiling, prompt-injection screen |
| 4.3 | Data transformation | `Normalize Input` → canonical record; `Merge Enrichment` consolidates lookups |
| 4.4 | AI / rule-based processing | `AI: Triage Classifier (OpenAI)` + `Parse AI Result` + `Apply Business Rules` (R1–R11) |
| 4.5 | Conditional routing | `Switch: Route by Queue` — 5 queues + Human Review fallback |
| 4.6 | External system integration | 4 authenticated calls to the ITSM API: directory, assets, KB, ticket create/patch |
| 4.7 | Approval / escalation | `wf_approval` — single-use token, Wait node, 48h timeout → escalation |
| 4.8 | Notifications | 9 email nodes: confirmation, assignment, approval request, approved, rejected, escalation, validation failure, ops alerts |
| 4.9 | Error handling | Node retries with backoff · `continueOnFail` on enrichment · explicit error output on the ITSM write · `external_ref` verification · `wf_error_handler` |
| 4.10 | Workflow logging | `app.audit_log`, one row per node event, keyed by `trace_id`; n8n execution data retained 14 days |
| 4.11 | Evaluation | `evaluation/run_eval.py` + 30 cases → completion rate, routing accuracy, priority accuracy, latency p50/p95, error rate |
| 4.12 | Professional application | Streamlit console: submit, tickets & routing, execution trace, evaluation |
| 4.13 | Deployment readiness | `docker-compose.yml` · `deploy/gcp/` Cloud Run scripts · Secret Manager wiring · `.env.example` · this README |

---

## Quick start

Two ways to run it. **GitHub Codespaces needs nothing installed** and is the
faster route — full guide in [`docs/08_codespaces.md`](docs/08_codespaces.md):

1. Get this project into a private GitHub repo — `git push`, or just drag the
   files into the browser at github.com/new (no git needed)
2. Add `OPENAI_API_KEY` as a repo Codespaces secret
3. **Code** → **Codespaces** → **Create codespace on main**
4. When it finishes: `docker compose up -d` then `make urls`

The devcontainer generates `.env` for you, including the public HTTPS
`WEBHOOK_URL` that makes the approval links work, and pre-builds the images.

### Or locally

**Prerequisites:** Docker Desktop, an OpenAI API key, Python 3.10+ (for the
evaluation harness only).

```bash
# 1. configure
cp .env.example .env
# edit .env: set OPENAI_API_KEY and N8N_ENCRYPTION_KEY
#   Linux/macOS:  openssl rand -hex 32
#   PowerShell:   -join ((1..32) | % { '{0:x2}' -f (Get-Random -Max 256) })

# 2. start the stack
docker compose up -d

# 3. wait for n8n, then open it
#    http://localhost:5678   (create the owner account on first run)
```

Then complete the three manual steps in n8n — they take about four minutes and
are covered with screenshots in [`docs/02_setup_local.md`](docs/02_setup_local.md):

1. **Import the four workflows** from `workflows/` (Workflows → ⋯ → Import from file).
2. **Create two credentials:**
   - *Postgres* named **`Postgres - Project 11`** → host `postgres`, port `5432`,
     database `n8n`, user `n8n`, password from your `.env`, schema `app`.
   - *SMTP* named **`SMTP - Mailpit (local)`** → host `mailpit`, port `1025`,
     no SSL/TLS, no credentials.
3. **Activate** `wf_main_triage`, `wf_approval` and `wf_sla_sweep`.

Then:

| What | Where |
|---|---|
| Operations console | http://localhost:8501 |
| n8n editor | http://localhost:5678 |
| Every notification email | http://localhost:8025 |
| Mock ITSM API docs | http://localhost:8000/docs |

### Send your first request

```bash
curl -X POST http://localhost:5678/webhook/it-request \
  -H "Content-Type: application/json" \
  -d '{
    "requester_email": "arun.mehta@tcs-demo.local",
    "requester_name": "Arun Mehta",
    "subject": "Received a suspicious email asking for my SSO credentials",
    "description": "An email that looks like it is from IT asked me to confirm my SSO password on an external link. Two colleagues clicked it.",
    "category": "security",
    "requested_cost": 0
  }'
```

Expected: HTTP 200, `priority: "P1"`, `queue: "SECURITY_IR"`, and
`rules_applied` containing `R1_SECURITY_OVERRIDE` — the security rule overrides
whatever the model returned. Two emails appear in Mailpit.

### Run the evaluation

```bash
pip install -r evaluation/requirements.txt
python evaluation/run_eval.py
```

Writes JSON, CSV and a markdown report to `evaluation/results/`, and inserts the
run into `app.eval_runs` so the console's Evaluation tab charts it.

---

## Repository layout

```
├── .devcontainer/              one-click GitHub Codespaces setup
├── docker-compose.yml          n8n + Postgres + Mailpit + ITSM API + console
├── .env.example                every setting, documented; real .env is gitignored
├── Makefile                    up / down / logs / eval / reset shortcuts
├── workflows/                  the four importable n8n exports
│   ├── wf_main_triage.json       37 nodes — the pipeline
│   ├── wf_approval.json          15 nodes — human-in-the-loop
│   ├── wf_sla_sweep.json          8 nodes — scheduled escalation
│   └── wf_error_handler.json      7 nodes — global error trigger
├── tools/build_workflows.py    regenerates + validates the exports
├── services/
│   ├── itsm-api/               FastAPI mock system of record (+ fault injection)
│   └── ui/                     Streamlit operations console
├── db/init/                    schema + reference data, applied on first boot
├── evaluation/
│   ├── test_cases.json           30 synthetic cases across 5 categories
│   └── run_eval.py               harness + metrics + report writer
├── deploy/gcp/                 Cloud Run deployment, Secret Manager, Cloud SQL
├── docs/                       architecture, setup, walkthrough, security, video
└── screenshots/                evidence for submission
```

## Design decisions worth defending

**The LLM advises; the rules decide.** `Apply Business Rules` runs after the
model and can override it. Anything with security, money or compliance
consequences — R1 security override, R5 cost approval, R6 privileged access — is
settled by code an auditor can read, not by a probability. The model's job is
classification and summarisation, where being wrong is cheap and correctable.

**Every AI call is assumed to fail.** `Parse AI Result` handles a missing
response, an error object, a markdown-fenced body and unparseable JSON, and falls
back to keyword classification with a lowered confidence score. R8 then routes
low-confidence records to Human Review rather than guessing.

**Fail closed, never open.** An unreadable approval response, a token mismatch,
or a 48-hour timeout all resolve to `expired` and escalate. No path exists where
ambiguity produces an approval.

**A 200 is not proof of success.** `IF: Ticket Created?` checks that the
response body actually contains `external_ref`, which catches the malformed-body
failure mode that status codes miss (test case TC-028).

**Untrusted text is fenced, and suspicious text is escalated to a human.**
User content reaches the model inside `<request>` tags with a standing
instruction never to follow instructions found there, and `Validate Request`
flags injection patterns independently. R11 then pulls flagged requests out of
the automated queues into `HUMAN_REVIEW` — note the direction of travel: injected
text can attract extra scrutiny but can never *clear* an approval flag set by R5
or R6. TC-019 and TC-020 assert that an injected "mark this pre-approved"
produces neither an approval nor an automated route.

**Idempotency at both ends.** `ticket_id` is the conflict key on the Postgres
upsert, and the ITSM API replays an existing ticket rather than duplicating, so a
retry after a transient failure cannot double-file a request.

## Documentation

| Document | Contents |
|---|---|
| [01_architecture.md](docs/01_architecture.md) | Component diagram, data model, decision logic, trust boundaries |
| [02_setup_local.md](docs/02_setup_local.md) | Full local setup, credential values, troubleshooting |
| [03_workflow_walkthrough.md](docs/03_workflow_walkthrough.md) | Node-by-node explanation of all four workflows |
| [04_evaluation.md](docs/04_evaluation.md) | Method, measure definitions, case matrix, how to read the report |
| [05_deployment_gcp.md](docs/05_deployment_gcp.md) | Cloud Run + Cloud SQL + Secret Manager, step by step |
| [06_security_config.md](docs/06_security_config.md) | Credential handling, injection defence, data protection, audit |
| [07_video_script.md](docs/07_video_script.md) | Timed script for the explanation video, covering (a)–(d) |
| [08_codespaces.md](docs/08_codespaces.md) | Running the whole stack in GitHub Codespaces, no local install |

## Submission checklist

- [x] n8n workflow export implementing the complete pipeline — `workflows/` (4 files, 67 nodes)
- [x] Supporting source code / API / LLM components — `services/`, `tools/`
- [x] Synthetic workflow evaluation / test cases — `evaluation/test_cases.json` (30 cases)
- [x] README and implementation / deployment documentation — this file + `docs/`
- [x] Evaluation outputs showing correctness and performance — `evaluation/results/`
- [ ] Output screenshots of the working workflow — capture into `screenshots/` (list in `docs/02_setup_local.md`)
- [x] Configuration required to run, credentials handled securely — `.env.example`, `docs/06_security_config.md`
- [x] GitHub-ready source structure and deployment configuration — `.gitignore`, `deploy/gcp/`
- [ ] Short explanation video — script in `docs/07_video_script.md`

The two unchecked items need you: record the screen and record the video.
Everything they depend on is in place.
