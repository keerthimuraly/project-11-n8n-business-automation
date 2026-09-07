# 08 · Running in GitHub Codespaces

The whole stack in your browser — no Docker Desktop, no WSL, nothing installed
locally. This is the path to use if local Docker is giving you trouble, and it
also gets the "GitHub-ready source structure" submission item done on the way.

Codespaces gives every forwarded port its own HTTPS URL, so n8n, the operations
console and Mailpit each open in a normal browser tab. That matters more than it
sounds: it makes the screenshots and the screen recording straightforward.

---

## What the devcontainer does for you

`.devcontainer/setup.sh` runs once when the Codespace is built and (via
`tools/make_env.sh`):

- generates `.env` with a fresh `N8N_ENCRYPTION_KEY`, Postgres password and ITSM
  API key — you never paste a generated secret by hand
- sets `WEBHOOK_URL`, `N8N_HOST`, `N8N_PROTOCOL` and `N8N_SECURE_COOKIE` to this
  Codespace's real public HTTPS address
- picks up `OPENAI_API_KEY` from a Codespaces secret if you set one
- installs the evaluation harness dependencies
- validates the four workflow exports and runs the decision-layer unit test
- pre-pulls and builds the images so your first `docker compose up -d` is quick

**Why the URL handling matters.** n8n builds webhook URLs and the approval
`$execution.resumeUrl` from `WEBHOOK_URL`. If that says `localhost`, the
Approve and Reject buttons in the approval email point at your own machine and
do nothing when clicked from a browser tab. Getting this right is the single
Codespaces-specific thing in the project, and the setup script handles it.

---

## Step 1 — put the project on GitHub

Two routes. Pick the second if you don't have git installed and don't want to
install it — everything after this step is identical.

### 1a · Browser only, no git, no local tools

1. Go to <https://github.com/new>
   - Repository name: `project-11-n8n-business-automation`
   - Select **Private**
   - Do **not** add a README or a .gitignore — the repo must be empty
   - **Create repository**
2. On the empty repo page, click **uploading an existing file**
3. Open the project folder on your computer, select **everything inside it**
   (Ctrl+A) and drag it onto the upload area. Wait for all files to list — about
   50 of them.
4. Type a commit message and click **Commit changes**

**Then check one thing.** GitHub's uploader sometimes skips folders whose name
starts with a dot. Look at your repo file list for a **`.devcontainer`** entry.

- **It's there** — perfect, carry on to Step 2.
- **It's missing** — no problem. Codespaces falls back to a default image that
  still has Docker, and the project is built to cope: in Step 5 you'll run
  `bash tools/make_env.sh` yourself, which does the same environment detection.
  Everything else works identically.

### 1b · With git installed

In a terminal in the project folder:

```bash
git init -b main
git add -A
git commit -m "Project 11: n8n Business Automation Pipeline"
```

Then create the remote. Easiest with the GitHub CLI if you have it:

```bash
gh repo create project-11-n8n-business-automation --private --source=. --push
```

If you don't have `gh`, create an empty **private** repo at
<https://github.com/new> — no README, no .gitignore — then:

```bash
git remote add origin https://github.com/<your-username>/project-11-n8n-business-automation.git
git push -u origin main
```

`.env` is gitignored, so no secret goes up. Worth confirming once:

```bash
git ls-files | grep "^\.env$"     # should print nothing
```

Keep the repo **private** until you're ready to submit.

## Step 2 — add your OpenAI key as a Codespaces secret

Do this before creating the Codespace and you never have to touch `.env`.

Repo → **Settings** → **Secrets and variables** → **Codespaces** → **New
repository secret**

- Name: `OPENAI_API_KEY`
- Value: your `sk-...` key

The setup script reads it automatically. It's stored encrypted by GitHub and
never appears in the repository.

> Skipping this is fine too. Without a key the pipeline still completes — the AI
> node fails, `Parse AI Result` falls back to keyword classification with a
> lowered confidence, and rules R8/R9 route those requests to Human Review. That
> graceful degradation is worth demonstrating deliberately at some point.

## Step 3 — create the Codespace

Repo → green **Code** button → **Codespaces** tab → **Create codespace on main**.

First build takes 3–5 minutes: it provisions the container, installs
Docker-in-Docker, then runs the setup script. Watch the terminal — you'll see it
detect the Codespace, write `.env`, validate the workflows, and pull images.

When it finishes you get a banner with your three URLs and the exact credential
values to type into n8n.

## Step 4 — start the stack

**If `.devcontainer` did not upload** (see Step 1a), generate `.env` first — this
is the step the devcontainer would otherwise have done for you:

```bash
bash tools/make_env.sh
```

It detects the Codespace, works out its public HTTPS URLs, and writes `.env`
with the correct `WEBHOOK_URL`. You'll also need the harness dependencies once:

```bash
pip install -r evaluation/requirements.txt
```

Then, either way:

```bash
docker compose up -d
docker compose ps
```

Five services, all running, postgres healthy. Then:

```bash
bash tools/show_urls.sh
```

(or `make urls` — same thing)

That reprints the URLs and the Postgres password you'll need in a moment. You
can also use the **PORTS** panel at the bottom of VS Code — click the globe icon
next to a port to open it.

## Step 5 — n8n setup

Open the n8n URL. On the first visit GitHub may show a "you are about to open a
forwarded port" interstitial — click through it.

1. **Create the owner account.** Local to your Codespace; anything you'll remember.

2. **Import the four workflows** — Workflows → ⋯ → **Import from File**, in this
   order so the sub-workflow reference resolves:

   1. `workflows/wf_error_handler.json`
   2. `workflows/wf_approval.json`
   3. `workflows/wf_sla_sweep.json`
   4. `workflows/wf_main_triage.json`

3. **Create two credentials**, names matching exactly:

   **`Postgres - Project 11`**

   | Field | Value |
   |---|---|
   | Host | `postgres` |
   | Database | `n8n` |
   | User | `n8n` |
   | Password | run `make urls` — it prints the generated one |
   | Port | `5432` |
   | Schema | `app` |
   | SSL | disable |

   **`SMTP - Mailpit (local)`**

   | Field | Value |
   |---|---|
   | Host | `mailpit` |
   | Port | `1025` |
   | User / Password | leave blank |
   | SSL/TLS | off |
   | Ignore SSL issues | on |

   Host is `postgres` / `mailpit`, not `localhost` — n8n reaches them over the
   Docker network, which is inside the Codespace.

4. **Activate** `wf_main_triage`, `wf_approval` and `wf_sla_sweep`. Leave
   `wf_error_handler` inactive — an error-trigger workflow fires when it is
   called, not when it is active.

## Step 6 — verify

```bash
make smoke
```

Sends the phishing scenario through the pipeline and pretty-prints the result.
You want `"priority": "P1"`, `"queue": "SECURITY_IR"` and
`R1_SECURITY_OVERRIDE` in `rules_applied`.

Then open the Mailpit URL — two emails — and the console URL, where the Tickets
tab shows the row and the Trace tab replays the audit trail.

## Step 7 — the approval demo

This is the strongest thing to record, and it works properly in Codespaces
because `WEBHOOK_URL` is set correctly.

1. Console → **Submit** tab → *Tableau licence INR 62,000* preset → **Submit**
2. n8n → **Executions**: one execution shows as **waiting**
3. Mailpit: the approval email → click **Approve**
4. n8n: the execution completes; Mailpit has the "Approved" email
5. Console → **Trace**: `APPROVAL_REQUESTED` then `APPROVAL_DECIDED`

## Step 8 — evaluation

```bash
make eval
```

30 cases, roughly a minute. Reports land in `evaluation/results/` and the
console's Evaluation tab picks them up.

---

## Things specific to Codespaces

**Ports are private by default.** Only your authenticated browser session can
reach them, which is what you want. If you need to show someone else, or call
the webhook from outside, right-click the port in the **PORTS** panel →
**Port Visibility** → **Public**. Set it back to private afterwards — a public
port on 5678 is an unauthenticated webhook open to the internet.

**Inside the Codespace, use `localhost`.** `make smoke` and `make eval` post to
`http://localhost:5678`, which skips GitHub's port-forwarding auth and is
faster. The public HTTPS URLs are for your browser.

**The Codespace stops after 30 minutes idle.** Your files and Docker volumes
survive; the containers do not. On reconnect:

```bash
docker compose up -d
```

Your n8n account, workflows and credentials are all still there — they live in
the Postgres volume.

**Free allowance.** 60 core-hours per month on the free plan, and this
devcontainer requests a 2-core machine, so that's about 30 hours of wall-clock
runtime. Plenty for building and recording. Stop the Codespace from the
<https://github.com/codespaces> page when you're done for the day rather than
leaving it idling — it counts hours while running, not while stopped.

**Rebuilding.** If you change `.devcontainer/*`, run **Codespaces: Rebuild
Container** from the command palette. `setup.sh` preserves your existing
`N8N_ENCRYPTION_KEY` and Postgres password on a rebuild, so your saved
credentials keep working. Regenerate `.env` by hand any time with:

```bash
bash tools/make_env.sh
```

**Deleting and recreating the Codespace** starts from a clean database — you
redo the n8n owner account, imports and credentials. Deleting is a full reset,
stopping is not.

---

## Troubleshooting

**Approval links do nothing.** Check `WEBHOOK_URL` in `.env` — it must be the
`https://...-5678.app.github.dev/` address, not localhost. Re-run
`bash tools/make_env.sh` then `docker compose up -d --force-recreate n8n`.

**n8n won't keep you logged in.** `N8N_SECURE_COOKIE` must be `true` when served
over HTTPS. The setup script sets this; if you overwrote `.env` by hand, fix it
and recreate the n8n container.

**Postgres nodes fail with "relation app.tickets does not exist".** The init
scripts only run on a fresh volume. Apply them manually:

```bash
docker compose exec -T postgres psql -U n8n -d n8n < db/init/001_schema.sql
docker compose exec -T postgres psql -U n8n -d n8n < db/init/002_seed.sql
```

**"No space left on device".** Codespaces gives 32 GB. Reclaim it:

```bash
docker system prune -af --volumes    # WARNING: also drops your n8n data
```

Or the safer version, which keeps volumes:

```bash
docker image prune -af
```

**Webhook returns 404.** `wf_main_triage` isn't Active, or you're hitting
`/webhook-test/` instead of `/webhook/`. The test path only works while the
editor is listening after you click Execute.

**Everything looks fine but no emails.** The SMTP credential name must be
exactly `SMTP - Mailpit (local)`, host `mailpit`, port `1025`, TLS off.

---

## Codespaces vs local vs GCP

| | Codespaces | Local Docker | GCP Cloud Run |
|---|---|---|---|
| Setup | push + click | Docker Desktop + WSL | `deploy/gcp/deploy.sh` |
| URLs for the demo | HTTPS per port, clickable | localhost | public |
| Mailpit (notification evidence) | yes | yes | not deployed |
| Cost | 60 free core-hours/month | free | ~₹3,500/month if left running |
| Counts as deployment evidence | no | no | **yes** |

For the assessment: **build and record in Codespaces**, then **run
`deploy/gcp/deploy.sh` once** so you have the Cloud Run deployment as evidence
for requirement 4.13. Say exactly that in the video — recording where Mailpit
makes the notification and approval paths visible, while deploying to Cloud Run
to prove deployment readiness, is a defensible engineering choice rather than a
shortcut.
