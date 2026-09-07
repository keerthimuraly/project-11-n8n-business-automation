# 02 · Local setup

Target: a working pipeline in about ten minutes, most of it waiting for Docker.

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker Desktop | Windows: enable the WSL 2 backend. Give it ≥ 4 GB RAM. |
| OpenAI API key | Any account with credit. `gpt-4o-mini` costs a fraction of a cent per request. |
| Python 3.10+ | Only for the evaluation harness and the workflow generator. |
| Git | For the source-control submission item. |

## Step 1 — configure

```bash
cd project-11-n8n-business-automation
cp .env.example .env          # Windows CMD: copy .env.example .env
```

Open `.env` and set two values:

```ini
OPENAI_API_KEY=sk-...your key...
N8N_ENCRYPTION_KEY=...32 bytes of hex...
```

Generate the encryption key:

```bash
# Linux / macOS / Git Bash
openssl rand -hex 32
```

```powershell
# Windows PowerShell
-join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) })
```

This key encrypts stored credentials. If you change it later, n8n can no longer
decrypt the credentials you saved and you will re-enter them — so set it once,
now, and keep it.

Everything else in `.env` has a working default. `APPROVAL_COST_THRESHOLD` and
`MIN_LLM_CONFIDENCE` are the two worth playing with during the demo.

## Step 2 — start the stack

```bash
docker compose up -d
docker compose ps          # all five services should be running/healthy
```

First run pulls four images and builds two, so allow a few minutes. Postgres
applies `db/init/*.sql` automatically on its first boot — that is when the `app`
schema and reference data appear.

Verify the supporting services before touching n8n:

```bash
curl http://localhost:8000/health                  # ITSM API -> {"status":"ok",...}
curl http://localhost:5678/healthz                 # n8n      -> {"status":"ok"}
```

Open <http://localhost:8025> — Mailpit's inbox, empty for now.

## Step 3 — n8n first-run

Open <http://localhost:5678> and create the owner account. It is a local
account in your own container; use anything you'll remember.

## Step 4 — import the workflows

For each of the four files in `workflows/`:

**Workflows** → **Create workflow** dropdown (or the ⋯ menu) → **Import from
File…** → pick the JSON → **Save**.

Import in this order, so the main workflow's reference to the approval
sub-workflow resolves on the first try:

1. `wf_error_handler.json`
2. `wf_approval.json`
3. `wf_sla_sweep.json`
4. `wf_main_triage.json`

Each import lands with its sticky notes intact — those notes are the narration
for your video, laid out over the canvas in reading order.

## Step 5 — create the two credentials

The workflow nodes reference credentials **by name**. Match these exactly and
every node connects with no further editing.

### Postgres — name it `Postgres - Project 11`

**Credentials** → **Add credential** → *Postgres*:

| Field | Value |
|---|---|
| Host | `postgres` |
| Database | `n8n` |
| User | `n8n` |
| Password | whatever you set as `POSTGRES_PASSWORD` in `.env` |
| Port | `5432` |
| Schema | `app` |
| SSL | disable |

`postgres`, not `localhost` — n8n reaches it over the compose network.
**Test** should come back green.

### SMTP — name it `SMTP - Mailpit (local)`

**Add credential** → *SMTP*:

| Field | Value |
|---|---|
| Host | `mailpit` |
| Port | `1025` |
| User | *(leave blank)* |
| Password | *(leave blank)* |
| SSL/TLS | off |
| Ignore SSL issues | on |

Mailpit accepts any sender and never delivers anything outward, which is exactly
what you want for a demo: nine notification paths you can screenshot, and no
risk of emailing a real address.

## Step 6 — activate

Open each of `wf_main_triage`, `wf_approval` and `wf_sla_sweep` and set the
**Active** toggle. Leave `wf_error_handler` inactive — an error-trigger workflow
fires when it is called, not when it is active.

> If `wf_main_triage` shows a warning on **Execute: Approval Sub-workflow**,
> click the node and re-pick `wf_approval - Manager Approval` from the list. This
> happens when the sub-workflow is imported after the main one.

## Step 7 — verify end to end

```bash
curl -X POST http://localhost:5678/webhook/it-request \
  -H "Content-Type: application/json" \
  -d '{
    "requester_email": "arun.mehta@tcs-demo.local",
    "requester_name": "Arun Mehta",
    "subject": "Account locked after password expiry",
    "description": "My corporate account is locked. I changed my password last week and now I cannot sign in to the intranet portal at all.",
    "category": "access",
    "requested_cost": 0
  }'
```

You should get back a JSON object containing `ticket_id`, `external_ref`,
`queue: "L1_SERVICE_DESK"`, `rules_applied`, and `processing_ms`.

Then check all four surfaces:

| Surface | What to look for |
|---|---|
| <http://localhost:8025> | Two emails: requester confirmation, queue assignment |
| <http://localhost:8501> | Tickets tab shows the row; Trace tab replays the audit rows |
| <http://localhost:5678> → Executions | A green execution with per-node data |
| `make audit` | `REQUEST_RECEIVED`, `TRIAGE_DECIDED`, `PIPELINE_COMPLETED` |

## Step 8 — exercise the approval path

Submit the Tableau licence scenario from the console's **Submit** tab
(₹62,000 → over threshold). Then:

1. Open Mailpit. There is an approval email addressed to the manager.
2. Click **Approve**. That link is the held execution's resume URL.
3. The main execution finishes; the requester gets an "Approved" email.
4. The console's Trace tab now shows `APPROVAL_REQUESTED` and
   `APPROVAL_DECIDED`, and `app.approvals` holds the decision.

This is the single best thing to record for the video — the pause and resume is
what "human-in-the-loop" actually means, and it is visible.

## Step 9 — exercise the failure paths

From the console's **Submit** tab, set **Fault injection** and submit:

| Setting | Expected |
|---|---|
| `500` | HTTP 502, ERROR audit row, alert email to IT Ops |
| `429` | Node retries 3× with backoff, then usually succeeds |
| `malformed` | HTTP 502 — caught by the `external_ref` check, not the status code |

Or with curl, add `"simulate_failure": "500"` to the body.

## Step 10 — run the evaluation

```bash
pip install -r evaluation/requirements.txt
python evaluation/run_eval.py
```

Roughly 30–90 seconds for 30 cases at concurrency 4. Output lands in
`evaluation/results/` as `EV-<timestamp>.json`, `.csv` and `.md`, and the
console's Evaluation tab picks it up.

The approval cases (TC-006, TC-007, TC-008, TC-009, TC-020, TC-023) leave held
executions behind — that is correct behaviour, and the harness scores them on
`approval_status: pending`. Clear them from n8n's Executions list, or approve
them from Mailpit, before the next run if you want a clean slate.

## Screenshots to capture for submission

The spec asks for output screenshots. These nine cover it:

1. `wf_main_triage` full canvas with sticky notes visible
2. A successful execution with the node data panel open on `Apply Business Rules`
3. `Switch: Route by Queue` showing the six output branches
4. Mailpit inbox with the full set of notification types
5. The approval email with Approve / Reject buttons
6. n8n executions list showing one held (waiting) execution
7. Console **Tickets** tab — queue load and priority mix charts
8. Console **Trace** tab — the audit trail expanded for one ticket
9. Console **Evaluation** tab — the metrics row after a run

Save into `screenshots/` with descriptive names (`01_main_canvas.png` and so on).

## Troubleshooting

**Webhook returns 404.** The workflow is not Active, or you are hitting
`/webhook-test/` instead of `/webhook/`. The test URL only works while the
editor is listening after you click Execute.

**Postgres nodes fail with "relation app.tickets does not exist".** The init
scripts ran before you added them, or on an older volume. Re-apply:

```bash
docker compose exec -T postgres psql -U n8n -d n8n < db/init/001_schema.sql
docker compose exec -T postgres psql -U n8n -d n8n < db/init/002_seed.sql
```

**`$env.OPENAI_API_KEY` is empty in the node.** `.env` was not present when the
container started. `docker compose up -d --force-recreate n8n` after fixing it.

**AI node returns 401.** The key is wrong or has no credit. The pipeline still
completes — `ai_status` becomes `fallback` and R8/R9 send it to Human Review,
which is worth demonstrating on purpose.

**Emails do not appear in Mailpit.** The SMTP credential name must be exactly
`SMTP - Mailpit (local)`, host `mailpit`, port `1025`, TLS off.

**Approval links do nothing.** `WEBHOOK_URL` in `.env` must match the address
your browser uses. If it is `http://localhost:5678/`, open the link from a
browser on the same machine.

**Everything is slow on Windows.** Docker Desktop with the WSL 2 backend and the
project on the Linux filesystem is markedly faster than a bind mount from
`C:\Users\...`. Not required, but noticeable.

**Start completely clean.**

```bash
docker compose down -v && docker compose up -d
```

Drops both volumes, so you re-run the n8n first-run setup and re-import.
