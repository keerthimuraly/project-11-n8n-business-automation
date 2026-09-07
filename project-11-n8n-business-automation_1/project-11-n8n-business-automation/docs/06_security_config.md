# 06 · Security and configuration

The submission requires configuration with credentials handled securely. This is
what that means here, and where the honest gaps are.

## 1. Credentials

**Nothing sensitive is in the repository or in a container image.** Three layers:

| Layer | Mechanism |
|---|---|
| Repository | `.gitignore` excludes `.env`, `.env.*` (except `.env.example`), `*.pem`, `*.key`, `credentials/`, `deploy/gcp/*-sa.json`, `service-account*.json` |
| Local runtime | Secrets live in `.env`, read by docker-compose, injected as container env vars |
| Cloud runtime | Secret Manager, mounted by Cloud Run via `--set-secrets`; the service account has `secretmanager.secretAccessor` and nothing broader |

Workflow nodes never contain a literal secret. Every one reads from the
environment:

```javascript
{{ $env.OPENAI_API_KEY }}    // Authorization header on the AI node
{{ $env.ITSM_API_KEY }}      // X-API-Key on all four ITSM calls
{{ $env.APPROVAL_COST_THRESHOLD }}
```

This is why `N8N_BLOCK_ENV_ACCESS_IN_NODE` is `false`. It is a deliberate
trade-off: it lets expressions read env vars, which also means anyone who can
edit a workflow in this n8n instance can read them. In a shared production
instance you would set it to `true` and move the API keys into n8n credential
objects instead. For a single-owner assessment instance, env access is the
simpler and more auditable choice — and worth stating out loud rather than
leaving implicit.

The Postgres and SMTP connections **do** use n8n credential objects, which are
encrypted at rest with `N8N_ENCRYPTION_KEY`.

### `N8N_ENCRYPTION_KEY`

Generated once and never changed. It encrypts every stored credential; rotating
it makes existing credentials undecryptable and they must be re-entered. In
Cloud Run it is stored in Secret Manager, not in an env file.

### Rotation

```bash
# OpenAI key, no redeploy needed
printf 'sk-newkey' | gcloud secrets versions add p11-openai-key --data-file=-
gcloud run services update p11-n8n --region=asia-south1 \
  --set-secrets="OPENAI_API_KEY=p11-openai-key:latest"
```

Locally: edit `.env`, then `docker compose up -d --force-recreate n8n`.

### If a secret is committed by accident

Rotate first, scrub second. Rewriting history does not un-leak a key that was
pushed. Revoke the OpenAI key in the dashboard, generate a new one, then clean
the history with `git filter-repo`.

## 2. Prompt injection

The LLM reads text written by whoever submitted the request. Three independent
defences, because any one of them can be beaten:

**Detection.** `Validate Request` screens for five patterns — "ignore previous
instructions", "disregard the system prompt", "you are now a…", "reveal your
prompt", "BEGIN SYSTEM" — and sets `injection_suspected`. It warns rather than
blocks, because a legitimate request can contain those words and a false
rejection is worse than a flagged one.

**Fencing.** User text reaches the model inside `<request>` tags, with a system
instruction stating that content between those tags is untrusted data and must
never be followed as an instruction.

**Structural containment — the one that actually matters.** Nothing the model
returns can grant authority. `requires_approval` is set by R5 and R6 from the
cost figure and keyword rules. The security override is R1. The priority floor is
R1 and R2. The model's output is whitelisted against fixed enums in
`Parse AI Result`, so even a compliant-looking injected response cannot introduce
a new category or a fabricated approval.

**Escalation.** R11 reroutes any request with `injection_suspected` to
`HUMAN_REVIEW` (security incidents still outrank it). The direction of travel
matters and is worth stating precisely: R11 can *add* scrutiny but never removes
it — it does not clear an approval flag set by R5 or R6, so injected text cannot
relax a control, only attract attention.

The first two defences reduce noise, and R11 puts a person in the loop. But the
reason the injection cannot succeed is structural: there is no code path that
reads an approval decision from model output.

TC-019 and TC-020 assert this. TC-020 submits a ₹80,000 request whose description
claims the manager already approved it, and asserts that approval is still
required and that the request is held for human review.

One honest wrinkle this testing surfaced: TC-019's injected text contains the
phrase "domain admin access", which trips R6's privileged-access keyword and
causes the request to *require* approval it would not otherwise have needed. That
is a false positive, and it is the safe direction — an attacker can add friction
to their own request but cannot remove it. Worth knowing about rather than
hiding: a determined nuisance could use it to generate approval noise, and the
fix would be to run R6's keyword scan against the validated subject and a
sanitised description rather than the raw text.

## 3. SQL injection

Every Postgres node uses `$1..$n` placeholders bound through
`options.queryReplacement`. No node builds SQL by concatenating a value. Even the
JSONB payloads go through `JSON.stringify()` into a bound parameter and are cast
with `::jsonb` server side.

## 4. The webhook is unauthenticated

Stated plainly because it is the largest gap. `POST /webhook/it-request` accepts
anything, from anywhere the URL is reachable. For a local demo that is correct —
adding auth would obscure the pipeline being assessed. For production it is not,
and the fix belongs at the edge:

- Header auth on the webhook node, or HMAC signature verification in a first Code
  node
- Cloud Armor or an API gateway in front, with rate limiting per source IP and
  per requester email
- mTLS if the caller is an internal system

The validation layer limits the damage in the meantime: field rules, length caps
(200 chars subject, 8,000 description), a cost ceiling, and no unbounded loops or
recursion anywhere in the workflow.

## 5. Approval integrity

| Property | How |
|---|---|
| Single use | A token is minted per approval and stored in `app.approvals` before the email is sent |
| Verified on resume | `Interpret Decision` compares the token in the link against the one minted for that execution; a mismatch resolves to `expired` |
| Fails closed | An unreadable response, a missing decision, a token mismatch, or a 48-hour timeout all resolve to `expired` and escalate. No path produces an approval from ambiguity. |
| Attributable | `decided_by`, `decided_at` and the comment are stored on the approval row and in the audit log |
| Time-bounded | `expires_at` on the row, `limitWaitTime` on the Wait node |

The remaining weakness: the approve link is a bearer URL in an email inbox.
Anyone with access to that inbox can approve. Real authorisation would put the
decision behind SSO — a signed link to a small approval page that authenticates
the manager before recording the decision. Worth naming in the video as the known
limitation.

## 6. Data protection

**No production data.** Every name, email address, employee id, asset tag,
department and cost figure is fabricated. All addresses use `@tcs-demo.local`,
which is not a routable domain. Mailpit never delivers outward, so no notification
can reach a real inbox from the local stack.

**What is stored.** `app.tickets` holds the requester's email, name, department,
manager and the request text — the minimum needed to route and notify. No
credentials, no payment data, no government identifiers.

**Retention.** n8n execution data is pruned after 336 hours (14 days) via
`EXECUTIONS_DATA_PRUNE`. Application tables have no automatic retention; a
production deployment needs a documented policy and a purge job.

**In transit.** OpenAI over HTTPS. Cloud SQL through the Cloud Run connector,
encrypted. Local service-to-service traffic on the Docker network is plain HTTP,
which is acceptable inside a single host and would need TLS across a real
network.

**Third-party exposure.** Request subject and description are sent to OpenAI.
That is a real data-residency consideration for a client engagement, and the
mitigations are: OpenAI's API does not train on API data by default; Azure OpenAI
in an Indian region would keep processing in-country; or a self-hosted model
removes the exposure entirely at the cost of classification quality. The
`AI: Triage Classifier` node is the single place that changes.

## 7. Least privilege

The Cloud Run service account has exactly three roles:
`roles/cloudsql.client`, `roles/secretmanager.secretAccessor`,
`roles/logging.logWriter`. No project editor, no storage admin.

The ITSM API is deployed with `--ingress=internal` and
`--no-allow-unauthenticated` — nothing outside the project can reach the mock
system of record. Only n8n is public, because webhooks require it.

The database user `n8n` owns both schemas. A tighter setup would give the
application a separate user with rights on `app` only, and n8n's own user rights
on `n8n_meta` only.

## 8. Audit

Every meaningful step writes to `app.audit_log` with `trace_id`, timestamp, node
name, event type, level and a JSONB payload. Given a trace id, the entire
decision can be reconstructed from the database without opening n8n:

```sql
SELECT ts, level, event, node_name, message, latency_ms
FROM app.audit_log
WHERE trace_id = 'TR-...'
ORDER BY ts, log_id;
```

Events: `REQUEST_RECEIVED`, `VALIDATION_FAILED`, `TRIAGE_DECIDED`,
`APPROVAL_REQUESTED`, `APPROVAL_DECIDED`, `INTEGRATION_FAILED`,
`PIPELINE_COMPLETED`, `SLA_BREACHED`, `UNHANDLED_ERROR`.

The log is append-only in practice — no workflow updates or deletes rows.

## 9. Configuration reference

Everything is environment-driven; nothing requires editing a workflow.

| Variable | Default | Effect |
|---|---|---|
| `OPENAI_API_KEY` | — | Required for AI classification. Absent → keyword fallback + Human Review. |
| `OPENAI_MODEL` | `gpt-4o-mini` | Any chat-completions model supporting JSON mode |
| `APPROVAL_COST_THRESHOLD` | `25000` | Inclusive. R5 requires approval at or above this. |
| `MIN_LLM_CONFIDENCE` | `0.55` | R8 sends anything below this to Human Review |
| `APPROVAL_TIMEOUT_HOURS` | `48` | Wait node limit before escalation |
| `ITSM_API_BASE` | `http://itsm-api:8000` | Point at a real ITSM to replace the mock |
| `ITSM_API_KEY` | `local-itsm-key` | Sent as `X-API-Key` on all four calls |
| `SERVICE_DESK_FROM` | `servicedesk@tcs-demo.local` | Sender on every notification |
| `IT_OPS_EMAIL` | `it-ops@tcs-demo.local` | Failure alerts; also the fallback approver for unknown requesters |
| `ESCALATION_EMAIL` | `it-head@tcs-demo.local` | Approval timeouts and SLA breaches |
| `WEBHOOK_URL` | `http://localhost:5678/` | **Must** match the public address, or approval resume links break |
| `N8N_ENCRYPTION_KEY` | — | Required. Set once; changing it invalidates stored credentials. |

The two worth changing live during a demo are `APPROVAL_COST_THRESHOLD` and
`MIN_LLM_CONFIDENCE` — drop the confidence floor to 0.9 and watch everything
route to Human Review, which shows the rules layer is real and not decoration.
