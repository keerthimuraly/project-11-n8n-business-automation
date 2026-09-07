# 01 · Architecture

## 1. The problem being solved

A TCS Enterprise IT service desk receives requests through several channels.
Each one is read by a human, mentally classified, given a priority, assigned to a
team, checked for whether it needs a manager's sign-off, keyed into the system of
record, and acknowledged. That sequence is repetitive, inconsistent between
agents, invisible while it is happening, and unrecoverable when a step is missed.

This project moves that sequence into an n8n workflow so that the decision is
made the same way every time, every step is recorded against a correlation id,
and the failure paths are explicit rather than "the ticket was never raised".

## 2. Components

| Component | Technology | Responsibility |
|---|---|---|
| Orchestration | n8n (Docker) | Every trigger, branch, integration and retry |
| Classification | OpenAI chat completions, JSON mode | Category, priority, sentiment, entities, summary |
| System of record | FastAPI mock ITSM | Directory, assets, knowledge base, ticket CRUD |
| Persistence | PostgreSQL 16 | `app.tickets`, `app.audit_log`, `app.approvals`, `app.eval_*` |
| Notification | SMTP → Mailpit locally | Nine notification paths |
| Demonstration UI | Streamlit | Submit, inspect routing, replay a trace, read evaluation |
| Evaluation | Python harness | 30 cases, five measures, JSON/CSV/markdown reports |

Postgres serves double duty: n8n keeps its own metadata in the `n8n_meta`
schema, the application keeps its tables in `app`. One container, two clearly
separated concerns.

The mock ITSM API exists because requirement 4.6 asks for meaningful external
integration, and a reproducible assessment cannot depend on a real ServiceNow
tenant. It behaves like the real thing in the ways that matter to the workflow:
it requires an API key, returns proper status codes, is idempotent on ticket
creation, and can be told to fail on demand so the error paths are demonstrable
rather than theoretical.

## 3. Diagram

Source: [`diagrams/architecture.mmd`](diagrams/architecture.mmd) — paste into
<https://mermaid.live> or open with the Mermaid VS Code extension.

## 4. The four workflows

| Workflow | Trigger | Nodes | Purpose |
|---|---|---|---|
| `wf_main_triage` | Webhook `POST /webhook/it-request` | 37 | The pipeline: intake → decision → integration → notification → response |
| `wf_approval` | Execute Workflow (called by main) | 15 | Human-in-the-loop authorisation with a 48-hour Wait and timeout escalation |
| `wf_sla_sweep` | Schedule, every 15 minutes | 8 | Finds overdue tickets, marks them breached, escalates, alerts |
| `wf_error_handler` | Error Trigger | 7 | Named as `errorWorkflow` on all three others; catches anything unhandled |

Splitting approval into its own workflow is deliberate. A Wait node holding for
48 hours inside the main canvas would mean the main execution stays open for two
days, which makes the executions list unreadable and couples the pipeline's
health to a manager's inbox habits. As a sub-workflow it holds its own execution
and returns a decision object.

## 5. Data model

```
app.queues          6 rows   routing targets, owner + escalation address
app.sla_policy      4 rows   response and resolve targets per priority

app.tickets                  one row per request that entered the pipeline
  ├─ correlation             ticket_id (PK), trace_id, external_ref
  ├─ requester context       enriched from the directory (dept, manager, VIP)
  ├─ raw request             subject, description, requested_cost, asset tag
  ├─ AI output               category, priority, sentiment, entities, confidence
  ├─ rules output            queue, routing_reason, rules_applied[], sla_due_at
  ├─ lifecycle               status, approval_status, closed_at, sla_breached
  └─ performance             processing_ms, error_count

app.audit_log                one row per node event, keyed by trace_id
app.approvals                single-use tokens, decisions, expiry
app.eval_runs / results      evaluation history for the console's charts
```

Two views support the console: `v_queue_load` (load, P1 count, breaches and mean
processing time per queue) and `v_ticket_trace` (log and error event counts per
ticket).

`trace_id` is the spine. It is minted in the first node, travels through every
subsequent node, is written to every audit row, and is returned to the caller.
Given a `trace_id` you can reconstruct the whole run from the database without
opening n8n.

## 6. Decision logic

The classifier proposes; twelve rules dispose. They run in order, and the first
rule to set a queue wins it.

| Rule | Condition | Effect |
|---|---|---|
| R1 | Security signal from the model **or** a keyword match | Force `P1` + `SECURITY_IR`. Non-negotiable. |
| R2 | Mass-impact language ("entire team", "production down") | Lift to `P1` |
| R3 | Requester is flagged VIP and not already `P1` | Lift priority one level |
| R4 | Requester not found in the directory | Flag `R4_UNVERIFIED_REQUESTER` |
| R5 | `requested_cost ≥ APPROVAL_COST_THRESHOLD` (default ₹25,000, inclusive) | Require approval |
| R6 | Privileged-access language (production DB, admin rights, domain admin, PII) | Require approval, default to `IAM_ACCESS` |
| R7 | No queue forced yet | Map category → queue |
| R7B | R7 chose `IAM_ACCESS` but the text is a credential/lockout request, and no approval is needed | Reroute to `L1_SERVICE_DESK` — IAM's queue is for entitlement changes, not password resets |
| R8 | `ai_confidence < MIN_LLM_CONFIDENCE` (default 0.55) and not security | Reroute to `HUMAN_REVIEW` |
| R9 | AI fell back **and** the request has a cost | Reroute to `HUMAN_REVIEW` |
| R11 | `injection_suspected` and not already `SECURITY_IR` | Reroute to `HUMAN_REVIEW`. Never clears an approval flag — injected text cannot relax a control, only attract scrutiny. |
| R10 | Knowledge base match is auto-resolvable, no approval needed, not P1, not held for review | Set `P4`, mark auto-resolved |

Every rule that fires is appended to `rules_applied`, returned in the API
response and stored on the ticket. When a reviewer asks "why did this go to
Security IR at P1", the answer is `R1_SECURITY_OVERRIDE`, not "the model
decided".

Then the SLA target is computed from the final priority — P1 240 min, P2 480,
P3 1440, P4 4320 — and both `sla_due_at` and `response_due_at` are stamped.

## 7. Trust boundaries

```
untrusted ──────────────────────────► trusted
requester text          validation + fencing         business rules
                        ↓                            ↓
      never an instruction to the LLM      never overridden by model output
```

Three things follow from this:

- **User text never becomes an instruction.** It reaches the model inside
  `<request>` tags, under a system instruction never to follow instructions found
  there. `Validate Request` independently flags injection patterns.
- **Model output never grants authority.** `requires_approval`, the security
  override and the priority floor are all set by rules. The model cannot mark
  something pre-approved because nothing reads an approval decision from it.
- **Approval decisions are token-bound.** The token is minted server side, stored
  before the email goes out, and verified on resume. A mismatch is treated as
  expiry.

## 8. Failure model

| Failure | Detection | Handling |
|---|---|---|
| Malformed request | `Validate Request` | 400 + explanatory email + WARN audit row, before any paid call |
| Directory / assets / KB unreachable | Node `continueOnFail` | Proceed unenriched; `enrichment_ok: false`; R4 flags it |
| OpenAI error, timeout or bad JSON | `Parse AI Result` | Keyword fallback, confidence lowered, R8/R9 route to Human Review |
| ITSM 5xx or 429 | 3 retries with 2 s backoff, then error output | ERROR audit row, page IT Ops, 502 to caller with the trace id |
| ITSM 200 with a malformed body | `IF: Ticket Created?` checks `external_ref` | Same failure path — a status code is not proof |
| Approver never responds | Wait node 48 h limit | Decision resolves to `expired`, escalate to the IT head |
| Tampered approval link | Token comparison in `Interpret Decision` | Treated as `expired` — fail closed |
| Anything unhandled, anywhere | `errorWorkflow` on every workflow | ERROR row under the original trace, error counter bumped, transient vs permanent split, alert |
| Ticket overdue | `wf_sla_sweep` every 15 min | Mark breached, escalate in ITSM, alert queue owner + IT head |
| Duplicate submission after a retry | `ON CONFLICT (ticket_id)` upsert + ITSM idempotent replay | One ticket, not two |

## 9. Performance characteristics

End-to-end latency is dominated by the OpenAI call. On `gpt-4o-mini` with
`temperature: 0` and a 500-token cap, expect roughly:

| Segment | Typical |
|---|---|
| Validation + normalisation | < 20 ms |
| Three enrichment calls (sequential) | 30–90 ms local |
| OpenAI classification | 600–2000 ms |
| Rules + routing | < 20 ms |
| ITSM write + Postgres upsert | 30–80 ms |
| Two notification emails | 40–120 ms |
| **Total, no approval** | **0.8–2.5 s** |

Validation rejections return in well under 100 ms because they exit before the
model call — deliberate, and the reason validation sits where it does.

Approval cases return once the manager decides, so their latency is human, not
technical; the evaluation harness scores those on `approval_status: pending`
rather than on elapsed time.

The enrichment calls are chained rather than parallel for readability on the
canvas. If latency mattered more than teaching clarity, they would fan out from
one node and merge — that is the first optimisation to make, and worth saying in
the video.

## 10. What would change for production

Honest list, since the assessment asks for deployment *readiness*, not a claim of
production completeness:

- Replace the mock ITSM API with the real ServiceNow / BMC connector; the
  workflow's four call sites are the only places that change.
- Move from `EXECUTIONS_DATA_SAVE_ON_SUCCESS: all` to `none` with a separate
  audit sink, once volume makes full retention expensive.
- Queue mode (`EXECUTIONS_MODE: queue`) with Redis and multiple workers for
  concurrency beyond a few requests per second.
- Real SMTP or Graph API instead of Mailpit; Slack or Teams for queue
  notifications.
- SSO in front of n8n, and a WAF or API gateway in front of the webhook with rate
  limiting per requester.
- Cost controls on the LLM: a per-day token budget and a cheaper model for the
  obvious cases, with the current model reserved for ambiguous ones.
