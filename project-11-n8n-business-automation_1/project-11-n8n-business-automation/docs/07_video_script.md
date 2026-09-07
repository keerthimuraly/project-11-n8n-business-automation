# 07 · Explanation video script

The spec asks the video to cover four things:

- **(a)** the automation problem, target workflow, architecture, main implementation choices
- **(b)** how a request flows through validation, AI/rules, routing, integrations, approvals, notifications
- **(c)** workflow testing, error handling, evaluation results
- **(d)** how the project is structured for source control, secure config, deployment

Target: **10–12 minutes**. Below is a timed running order with what to show and
roughly what to say. Speak it, don't read it.

---

## Before you record

Have all of this open in tabs, in this order:

1. VS Code on the project root
2. n8n editor with `wf_main_triage` open, canvas zoomed to fit
3. The console at `localhost:8501`
4. Mailpit at `localhost:8025` — **empty it first**
5. A terminal in the project root
6. `evaluation/results/EV-....md` from a completed run

Then:

- Run `docker compose up -d` and confirm all five services are healthy
- Run the evaluation once so the console's Evaluation tab has data
- Clear old executions from n8n so the list is readable
- Set your display to 1080p and increase the terminal font size

---

## (a) Problem, architecture, choices — 0:00 to 2:30

**0:00 — Open on the problem, not the tool.**

> "A TCS Enterprise IT service desk receives requests through several channels.
> Each one is read by a person, mentally classified, given a priority, assigned
> to a team, checked for whether it needs a manager's sign-off, keyed into the
> system of record, and acknowledged. That sequence is repetitive, inconsistent
> between agents, invisible while it is happening, and unrecoverable when a step
> is missed. This project moves it into an n8n workflow."

**0:30 — Show the architecture diagram** (`docs/diagrams/architecture.mmd`
rendered).

Name the components in one pass: n8n orchestrates, OpenAI classifies, a FastAPI
service stands in for the system of record, Postgres holds tickets and the audit
trail, Mailpit catches every notification, Streamlit is the operations console.

**1:15 — State the central design decision, because it is the thing that
distinguishes this from a demo.**

> "The most important choice in this project is that the LLM advises and the
> rules decide. The model classifies and summarises — where being wrong is cheap
> and correctable. Anything with security, money or compliance consequences is
> settled by deterministic code an auditor can read. You'll see that in a moment
> when a phishing report gets forced to P1 regardless of what the model returned."

**1:45 — Show the four workflows in the n8n sidebar.** Main pipeline, approval
sub-workflow, SLA sweep on a schedule, global error handler. Say why approval is
separate: a Wait node holding for 48 hours inside the main canvas would keep the
main execution open for two days.

---

## (b) The flow — 2:30 to 6:30

**2:30 — Full canvas of `wf_main_triage`,** sticky notes visible.

> "Thirty-seven nodes in five stages. The sticky notes on the canvas name the
> requirement each stage satisfies."

**2:50 — Walk the canvas left to right.** Don't open every node — point and name:

- Webhook → Normalize (mints `trace_id` and `ticket_id`)
- **Validate** — pause here: "field rules, formats, a cost ceiling, and a
  prompt-injection screen. It runs *before* the enrichment and the AI call, so a
  malformed request never spends an OpenAI token."
- Three enrichment calls — "directory, assets, knowledge base. All three continue
  on failure: a directory outage degrades this to unenriched triage, it doesn't
  stop it."
- AI classifier — "strict JSON mode, temperature zero, three retries"
- **Open `Parse AI Result`.** "This is the node I'd most want you to look at. It
  handles a missing response, an error object, a markdown fence around the JSON,
  and outright unparseable output — and falls back to keyword classification with
  a lowered confidence. Every AI call in this pipeline is assumed to fail."
- **Open `Apply Business Rules`.** Scroll through R1 to R11. "Twelve rules. Every one
  that fires is recorded, returned to the caller, and stored on the ticket. When
  someone asks why this went to Security IR at P1, the answer is
  `R1_SECURITY_OVERRIDE` — not 'the model decided'."
- Switch — six outputs, five queues plus a Human Review fallback

**4:30 — Now run one live.** Console → Submit tab → the phishing preset →
Submit.

Point at the response as it lands:

> "P1. Security IR. And `rules_applied` shows `R1_SECURITY_OVERRIDE` — the rule
> overrode whatever the model returned. Eight hundred milliseconds."

**5:00 — Mailpit.** Two emails: requester confirmation and the queue assignment
handover with the routing reason and the rules that fired.

**5:20 — The approval path. This is the strongest thing you have; give it time.**

Console → Submit → the Tableau licence preset (₹62,000) → Submit.

> "Sixty-two thousand rupees, above the twenty-five thousand threshold. R5 fires
> and the request needs approval."

Show n8n's executions list: **one execution is *waiting*.**

> "That execution is genuinely paused. n8n has persisted it and it will sit there
> for up to forty-eight hours."

Mailpit → the approval email. Show that it gives the manager the cost, the
priority, the AI summary and the reason approval is needed — enough to decide
from the inbox. Click **Approve**.

Back to the executions list: it has completed. Mailpit has the "Approved" email.

> "The approve link is that execution's resume URL, carrying a single-use token
> that was stored before the email went out. A token mismatch, an unreadable
> response, or a timeout all resolve to expired and escalate. There is no path
> where ambiguity produces an approval — it fails closed."

**6:10 — Console → Trace tab.** Expand the audit trail for that ticket.

> "Every node wrote a row keyed by the same trace id. Given a trace id you can
> reconstruct the whole decision from the database without opening n8n."

---

## (c) Testing, error handling, evaluation — 6:30 to 9:30

**6:30 — Error handling, demonstrated rather than described.**

Console → Submit → any preset → set **Fault injection** to `500` → Submit.

> "The mock system of record returns a 500. The node retried three times with
> backoff, then took its error output."

Show the HTTP 502 response. Then Mailpit: the alert to IT Ops. Then the console
Trace tab: an ERROR row.

> "The caller gets a 502 with the trace id — the request was captured, it just
> wasn't filed, and IT Ops has been paged."

**7:15 — The malformed case, because it is the subtle one.** Fault injection →
`malformed` → Submit.

> "This one returns HTTP 200 with an HTML error page. A status-code check would
> sail straight past it. The `IF: Ticket Created?` node checks the response body
> actually contains an external reference — because a 200 is not proof of
> success."

**7:45 — Show `wf_error_handler`.**

> "This is named as the error workflow on all three others, so anything
> unhandled anywhere lands here with full context. It writes an ERROR row under
> the original trace id, bumps the ticket's error counter, and splits transient
> network failures from permanent ones. It's the last line, not the only line —
> retries, continue-on-fail, and the explicit error output all come first."

**8:15 — Evaluation.** Terminal: `python evaluation/run_eval.py`.

While it runs, explain the case matrix: 30 cases across happy path, edge,
negative, security and error handling.

Call out three specific cases:

> "TC-023 and TC-024 are twenty-five thousand and twenty-four thousand nine
> hundred ninety-nine — they pin the threshold comparison as inclusive. An
> off-by-one there is a real-money bug.
>
> TC-019 and TC-020 are prompt-injection attempts. TC-020 submits an eighty
> thousand rupee request whose description claims the manager already approved
> it, and asserts approval is still required. That's the security property the
> whole design exists to guarantee.
>
> TC-028 is the malformed-body case you just saw."

**9:00 — Read the results off the terminal**, then the console's Evaluation tab
with the metrics row and the trend chart.

Define the measures in one breath: completion rate, routing accuracy, priority
accuracy, error rate, latency p50 and p95.

**Be straight about the limits.** It reads as competence, not weakness:

> "Two honest limits. The approval cases aren't driven to completion — they're
> scored on the pending state, and testing the full cycle needs the resume URL
> from the sent mail. And thirty cases against a non-deterministic model is a
> small sample; I'd run it three times before drawing conclusions about a prompt
> change."

---

## (d) Structure, config, deployment — 9:30 to 11:30

**9:30 — VS Code, the folder tree.**

> "Workflows are exported as importable JSON. They're generated by
> `tools/build_workflows.py`, not hand-edited — connections in an n8n export are
> keyed by node name and easy to break by hand."

Run `python tools/build_workflows.py --check`.

> "It validates that every connection points at a node that exists, that each
> workflow has a trigger, and that nothing is unreachable."

**10:00 — Secure configuration.** Open `.env.example`, then `.gitignore`.

> "Nothing sensitive is in the repository. `.env` is gitignored; only the example
> is committed. Workflow nodes never contain a literal secret — every one reads
> from the environment. In Cloud Run those come from Secret Manager, mounted by
> the runtime, and the service account has exactly three roles."

Show a header expression: `{{ $env.OPENAI_API_KEY }}`.

Then the trade-off, in one sentence:

> "That requires env access in expressions, which means anyone who can edit a
> workflow here can read those values. For a single-owner assessment instance
> that's the simpler and more auditable choice; in a shared production instance
> you'd move them into n8n credential objects instead."

**10:45 — Deployment.** Open `deploy/gcp/deploy.sh` and scroll it.

> "Cloud SQL, Secret Manager, a least-privilege service account, and two Cloud Run
> services. Two details that are easy to get wrong: n8n is deployed twice,
> because it needs its own public URL to build correct webhook and approval
> resume URLs and that isn't known until the service exists. And minimum
> instances is one, not zero — Cloud Run scaling to zero would mean the schedule
> trigger never fires and held approval executions are lost."

**11:15 — Close on the choice you made and why.**

> "I deployed to Cloud Run because the script is what proves deployment
> readiness, and I demoed from local Docker because Mailpit makes the
> notification and approval paths visible. The documentation covers the
> production gap honestly — the webhook is unauthenticated, which is right for a
> demo and wrong for production, and the fix belongs at the edge with an API
> gateway and HMAC verification."

---

## Recording notes

- **OBS** or Windows **Game Bar** (`Win+G`) both work. Record at 1080p.
- Zoom the n8n canvas so node names are legible — unreadable node names are the
  most common problem with workflow demo videos.
- Increase your terminal font before recording.
- Trim the OpenAI wait if a call takes more than a couple of seconds.
- Record in the four sections above and stitch them. Re-recording one section
  beats re-recording twelve minutes.
- If a live run fails on camera, keep it and narrate what happened — you built
  the error paths precisely so that a failure is legible. A demo that recovers
  visibly is stronger than one that never stumbles.
- Say numbers out loud when they appear: "eight hundred milliseconds",
  "ninety-five percent routing accuracy". Reviewers listen more than they read.

## One-minute version

If you also need a short cut, keep only:

1. The problem, in one sentence (0:15)
2. The canvas, full width (0:10)
3. The phishing request → P1 → `R1_SECURITY_OVERRIDE` (0:20)
4. The approval email → Approve → execution resumes (0:15)
