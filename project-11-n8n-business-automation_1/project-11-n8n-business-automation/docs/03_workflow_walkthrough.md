# 03 · Workflow walkthrough

Node by node, in execution order. Use this as the narration for the video and as
the reference when a reviewer asks what a specific node does.

---

## `wf_main_triage` — 37 nodes

### Intake and validation

**`Webhook: IT Request`** — `POST /webhook/it-request`, `responseMode:
responseNode`, so the HTTP connection stays open until a Respond node closes it.
That is what lets the caller receive the full decision trace instead of a bare
acknowledgement.

**`Schedule: Manual Backlog Sweep`** — a second trigger type, disabled by
default, present to show that the same downstream chain can be driven by a
schedule as well as a webhook. The real scheduled work lives in `wf_sla_sweep`.

**`Normalize Input`** (Code) — requirement 4.3. Reads from `.body` (webhook) or
the top level (manual run), coerces every field, collapses whitespace, caps
lengths, and mints the two correlation ids:

- `ticket_id` — `INC-2026-<5 digits><4 chars>`, human-readable, the primary key
- `trace_id` — `TR-<base36 epoch>-<4 chars>`, written to every audit row

It also stamps `started_at_ms`, which is what `Finalise Record` later subtracts
to produce the end-to-end latency measure.

**`Log: Request Received`** (Postgres) — the first audit row. Note it is a
`RETURNING` insert with `$1..$7` placeholders bound through `queryReplacement`:
parameterised SQL, not string interpolation, everywhere in this project.

**`Validate Request`** (Code) — requirement 4.2. Required fields, email format,
minimum lengths, category and priority enum checks, a cost ceiling, and five
prompt-injection regexes. Errors block; unrecognised category or priority become
warnings and let the AI decide instead.

The order matters: validation runs **before** the enrichment and AI nodes, so a
malformed request never spends an OpenAI token or touches the system of record.

**`IF: Request Valid?`** — the first branch.

*False branch:* `Log: Validation Failed` (WARN) → `Email: Missing Information`
(lists exactly which fields failed) → `Respond: 400 Invalid`. A rejection is a
designed outcome with a notification and an audit row, not a dropped request.

### Enrichment — requirement 4.6

Three authenticated `GET`s to the ITSM API, each carrying
`X-API-Key: {{ $env.ITSM_API_KEY }}`:

| Node | Endpoint | Adds |
|---|---|---|
| `HTTP: Lookup Employee` | `/directory/employees/{email}` | department, location, manager, cost centre, VIP flag |
| `HTTP: Lookup Assets` | `/directory/assets?email=` | assigned devices, OS, warranty |
| `HTTP: Search Knowledge Base` | `/kb/search?q=` | best-matching article, whether it is auto-resolvable |

All three are set to `onError: continueRegularOutput` with retries. A directory
outage degrades the pipeline to unenriched triage; it does not stop it.

**`Merge Enrichment`** (Code) — pulls all three results via `$('Node Name')`
inside try/catch and produces one flat working record. Sets `enrichment_ok` so
downstream rules know whether the context is trustworthy.

### AI classification — requirement 4.4

**`AI: Triage Classifier (OpenAI)`** — `POST
https://api.openai.com/v1/chat/completions` with:

- `temperature: 0` — the same request classifies the same way twice
- `response_format: { type: "json_object" }` — strict JSON mode
- `max_tokens: 500` — a cost ceiling per call
- 3 retries, 30 s timeout, `continueRegularOutput` on failure

The system message specifies the exact keys required and the priority
definitions. The user message carries the enrichment context, then the request
text fenced in `<request>` tags with a standing instruction never to follow
instructions found inside them.

**`Parse AI Result`** (Code) — the defensive layer, and the node most worth
explaining in the video. It handles: a `.error` object in the response, missing
`choices[0].message.content`, a markdown fence wrapped around the JSON, and
outright unparseable output. Any of those sets `ai_status: "fallback"` and falls
back to keyword classification with a confidence of 0.3. It then whitelists
every enum value, so an unexpected category or priority from the model cannot
propagate.

**`Apply Business Rules`** (Code) — requirements 4.4 and 4.5. Twelve rules, R1–R11 (R7 has a sub-rule R7B),
documented in [01_architecture.md §6](01_architecture.md#6-decision-logic). Every
rule that fires appends its code to `rules_applied`, which is returned to the
caller and stored on the ticket. This is the node that makes the decision
auditable: `R1_SECURITY_OVERRIDE` is a reason a person can check, and
"the model said so" is not.

It also computes `sla_due_at` and `response_due_at` from the final priority.

**`Log: Triage Decision`** (Postgres) — the decision is logged *before* any side
effect, at INFO or WARN depending on `ai_status`, with the rules, confidence and
reason in the JSONB payload.

### Routing — requirement 4.5

**`Switch: Route by Queue`** — six outputs. Five string-equality rules on
`$json.queue` plus `fallbackOutput: extra` renamed "Human Review", so a queue
value nobody anticipated cannot fall off the end of the workflow.

Each branch has its own `Set:` node stamping `queue_display_name`,
`assignee_email` and `routed_at`. They all converge on the integration node. The
per-branch nodes are partly for the canvas — six visible paths read better in a
demo than one — and partly the natural extension point when a queue needs
queue-specific fields.

### Integration — requirement 4.6

**`HTTP: Create Ticket in ITSM`** — `POST /tickets`, 3 retries with 2 s backoff,
`onError: continueErrorOutput`. It forwards `X-Simulate-Failure` from the request
so the failure paths can be triggered on demand.

**`IF: Ticket Created?`** — checks `external_ref` is non-empty. This exists
because a 200 response is not proof of success: the malformed-body failure mode
returns HTTP 200 with an HTML error page, which a status-code check would sail
straight past. TC-028 asserts this catch.

Both the HTTP node's error output and this IF's false branch feed the same
failure path:

`Log: Integration Failure` (ERROR) → `Email: Alert IT Ops` → `Respond: 502
Integration Error` — which tells the caller the request was captured but not
filed, and returns the trace id so it can be followed up.

### Approval — requirement 4.7

**`IF: Requires Approval?`** reads the flag set by R5 or R6.

**`Execute: Approval Sub-workflow`** with `waitForSubWorkflow: true` — the main
execution waits for the decision object. Twelve fields are mapped explicitly
rather than passing the whole record, which keeps the sub-workflow's contract
readable.

### Persistence, notification, response

**`Finalise Record`** (Code) — reads back from the integration and approval
nodes, decides the terminal `status` (Approved / Rejected / Escalated / Resolved
/ Routed), computes `processing_ms`, and builds the `api_response` object that
requirement 5 specifies.

**`Postgres: Upsert Ticket`** — 36 columns, `ON CONFLICT (ticket_id) DO UPDATE`.
Idempotent by design: a replay updates the existing row rather than failing or
duplicating.

**`Email: Notify Requester`** — confirmation table with ticket, reference,
category, priority, team, status, SLA target, the AI summary, and the KB
resolution when one applies.

**`Email: Notify Assigned Queue`** — the working handover: requester context
including the VIP flag, response deadline, AI summary, routing reason, the rules
that fired, and the assets on record.

**`Log: Pipeline Completed`** → **`Respond: 200 Accepted`** — the audit row
closes the trace, then the response object goes back to the caller.

### Sticky notes

Six notes are laid out across the canvas in reading order, one per stage, each
naming the requirements it satisfies. They import with the workflow and are
designed to be visible in a full-canvas screenshot.

---

## `wf_approval` — 15 nodes

**`When Called by Main Workflow`** — `executeWorkflowTrigger` with twelve typed
inputs, so the contract is explicit in the UI.

**`Prepare Approval Request`** (Code) — mints `approval_id` and a single-use
token from three entropy sources, and computes the expiry from
`APPROVAL_TIMEOUT_HOURS`.

**`Postgres: Record Approval Request`** — the token is persisted **before** the
email is sent. If the email fails, the pending approval still exists and is
visible.

**`Email: Approval Request to Manager`** — the hinge of the whole
human-in-the-loop pattern. The Approve and Reject buttons are built from
`{{ $execution.resumeUrl }}`, carrying `decision`, `token` and `approver` in the
query string. It shows the manager the cost, the priority, the AI summary and
why approval is required — enough to decide from the email without opening a
tool.

**`Wait: Manager Decision`** — `resume: webhook`, `limitWaitTime: true`,
48 hours. The execution genuinely pauses; n8n persists it and resumes on the
inbound request. In the executions list it appears as *waiting*, which is the
screenshot worth taking.

**`Interpret Decision`** (Code) — reads `decision` from query or body, maps
several spellings to approve/reject, and **verifies the token against the one
minted for this execution**. A mismatch, an unreadable response, or a timeout all
resolve to `expired`. There is no path where ambiguity produces an approval.

**`Postgres: Save Decision`** and **`Log: Approval Decided`** — the decision and
the audit row, WARN on expiry.

**`HTTP: Update Ticket Status`** — pushes the outcome back to the system of
record: status, approval status, approver, escalation flag, comment.

**`Switch: Decision Outcome`** → three notification paths:

| Output | Node | Recipient |
|---|---|---|
| Approved | `Email: Approved` | requester |
| Rejected | `Email: Rejected` | requester, with the approver's note |
| Expired (fallback) | `Email: Escalate on Timeout` | `ESCALATION_EMAIL` |

**`Return to Main Workflow`** (Code) — returns a compact decision object so the
main workflow can finish its record.

---

## `wf_sla_sweep` — 8 nodes

**`Schedule: Every 15 Minutes`** → **`Postgres: Find Breached Tickets`** — open
tickets past `sla_due_at`, not already flagged, with `minutes_overdue` computed
in SQL, limited to 50 per sweep so one bad night cannot produce a mail storm.

**`IF: Any Breaches?`** → **`Postgres: Mark Breached`** (sets `sla_breached` and
status `Escalated`) → **`HTTP: Escalate in ITSM`** → **`Email: SLA Breach
Alert`** (queue owner + IT head) → **`Log: SLA Escalation`** (WARN).

The false branch is a NoOp, so a quiet sweep is a clean green execution rather
than a failure.

This is the piece that makes the automation proactive: nothing has to arrive for
the system to notice a problem.

---

## `wf_error_handler` — 7 nodes

Named as `errorWorkflow` in the settings of all three other workflows, so any
unhandled node failure anywhere lands here with the full execution context.

**`Extract Error Context`** (Code) — normalises n8n's error payload into flat
fields (workflow, failed node, message, stack, execution URL) and classifies
severity: a message matching timeout / ECONNREFUSED / ENOTFOUND / socket /
network is `TRANSIENT` with `retry_advised: true`; everything else is
`PERMANENT`.

**`Log: Unhandled Error`** — an ERROR row under the original `trace_id` where one
survived, so the failure sits alongside the successful steps of the same request.

**`Postgres: Increment Error Count`** — bumps `tickets.error_count`, which the
console surfaces.

**`IF: Transient Failure?`** → `Email: Transient Failure Digest` (IT Ops, with
re-run guidance) or `Email: Permanent Failure Alert` (IT Ops + IT head, with the
error description).

This workflow is the last line of defence, not the only one. Node-level retries,
`continueOnFail` on enrichment, the explicit error output on the ITSM write and
the `external_ref` verification all come first. By the time execution reaches
here, something genuinely unanticipated has happened — and it is still recorded
and alerted rather than silent.

---

## Regenerating the exports

The JSON files are generated, not hand-edited:

```bash
python tools/build_workflows.py          # rewrite all four
python tools/build_workflows.py --check  # validate only
```

The generator validates that every connection points at a node that exists, that
each workflow has a trigger, that no node names collide, and that no non-trigger
node is unreachable. Editing the JSON by hand is what breaks name-keyed
connections; changing the generator and re-emitting does not.

If you edit a workflow in the n8n UI and want to keep the change, export it from
n8n over the file in `workflows/` — and note that the generator will overwrite it
next time it runs.
