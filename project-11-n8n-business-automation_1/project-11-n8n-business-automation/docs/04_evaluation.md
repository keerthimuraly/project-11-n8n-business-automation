# 04 · Evaluation

Requirement 4.11 asks for workflow correctness and system performance measured
with repeatable test cases. This is the method, the measures and how to read the
output.

## Method

`evaluation/run_eval.py` posts each case in `test_cases.json` to the live webhook
and compares the response against declared expectations. It is a black-box test:
it exercises the deployed pipeline through the same interface a corporate portal
would use, so it validates the whole chain — validation, enrichment, the LLM
call, the rules, routing, integration and the response contract — not the
individual Code nodes in isolation.

Each case declares only the expectations that matter for what it tests, and each
dimension is scored independently. A case that routes correctly but assigns the
wrong priority still counts toward routing accuracy. Collapsing everything into
one pass/fail number would hide exactly the behaviour worth knowing about.

Before each run the harness resets the mock ITSM store, so ticket references are
comparable between runs.

## The measures

| Measure | Definition | Where it comes from |
|---|---|---|
| **Completion rate** | Share of cases that reached a defined terminal state — HTTP 200, 400 or 502 | A deliberate 400 or 502 *is* completion; the pipeline decided and said so. A timeout, a 500 or an unparseable body is not. |
| **Routing accuracy** | Correct queue ÷ cases that declare an expected queue | The core correctness measure for the automation's purpose |
| **Priority accuracy** | Correct priority ÷ cases that declare an expected priority | Scored separately because priority is partly model judgement while routing is mostly rules |
| **Error rate** | Cases with a client error, a timeout or an HTTP 500 ÷ total | Only *unexpected* failures. The fault-injection cases expect a 502 and do not count here. |
| **Processing latency** | Mean, p50, p95 and max, client-observed end to end, plus the server-reported `processing_ms` | Client-side includes network and n8n queueing; the server figure isolates workflow time |
| **All-assertion pass rate** | Cases where every declared assertion held | The strictest number, and the one to quote as the headline |

Counting a designed 400 as *completed* is a deliberate choice. Rejecting a
malformed request quickly, with an explanation and an audit row, is the pipeline
working. Treating that as a failure would reward a system that silently accepted
bad input.

## Case matrix — 30 cases

| Category | Count | What it proves |
|---|---|---|
| `happy_path` | 10 | Correct routing across all five queues, and the approval trigger on both cost and privileged access |
| `edge` | 9 | VIP bump, mass-impact language, threshold boundaries (₹24,999 vs ₹25,000 vs ₹62,000), vague text → Human Review, unknown requester, user category overridden by the classifier |
| `negative` | 5 | Validation rejects missing email, malformed email, short description, missing subject, absurd cost — all before any AI call |
| `security` | 3 | Phishing and ransomware force P1 → Security IR; two prompt-injection attempts fail to change routing or fabricate an approval |
| `error_handling` | 3 | Forced 500 → 502; forced 429 → retries recover; forced malformed body → caught by the `external_ref` check |

Some cases are worth knowing individually:

- **TC-023 / TC-024** — ₹25,000 and ₹24,999. Together they pin the threshold
  comparison as inclusive. An off-by-one here is a real-money bug.
- **TC-019 / TC-020** — injected instructions telling the pipeline to mark the
  request pre-approved. Both assert that no approval is produced and that R11
  pulls the request into `HUMAN_REVIEW`. This is the security property the design
  exists to guarantee, and the assertion is deliberately about what the injection
  *cannot* achieve rather than about an exact queue the model might vary.
- **TC-025** — a VIP reporting a security incident, so R1 and R3 both apply. It
  asserts security wins the queue and P1 is not double-bumped.
- **TC-028** — the malformed-body case. It is the only one that a status-code
  check would pass and the `external_ref` check catches.
- **TC-012** — deliberately vague text, asserting the low-confidence route to
  Human Review rather than a confident wrong answer.

All data is synthetic: fabricated names, `@tcs-demo.local` addresses, invented
asset tags and cost figures. No production or client data appears anywhere.

## The fast loop: `tools/test_rules.js`

Before the end-to-end harness there is a unit test for the decision layer:

```bash
node tools/test_rules.js
```

It pulls the `Parse AI Result` and `Apply Business Rules` Code nodes straight out
of `wf_main_triage.json`, feeds them every case from `test_cases.json` with a
stubbed well-behaved model, and asserts the queue, priority, approval and rule
expectations. No Docker, no OpenAI key, no network — it runs in about a second.

This is the test that earns its keep. It caught two real defects during
development: `access`-category password resets were routing to IAM instead of L1
(fixed by R7B), and injected text was reaching the routing decision unflagged
(fixed by R11). Both were invisible to a black-box run against a live model,
because a competent model produced plausible-looking output either way.

Use it whenever you change a rule. Use `run_eval.py` to validate the deployed
pipeline.

## Running the end-to-end harness

```bash
pip install -r evaluation/requirements.txt

python evaluation/run_eval.py                        # all 30
python evaluation/run_eval.py --kind security        # one category
python evaluation/run_eval.py --case TC-019 --verbose
python evaluation/run_eval.py --concurrency 1        # serial, easier to watch
python evaluation/run_eval.py --no-db                # skip the Postgres write
```

Exit code is 0 only when every assertion in every case passed, so it drops
straight into CI.

Roughly 30–90 seconds for the full set at concurrency 4, dominated by the OpenAI
calls.

## Output

Three files per run in `evaluation/results/`, named `EV-<timestamp>-<id>`:

| File | Use |
|---|---|
| `.md` | The submission artefact — headline measures, per-category breakdown, per-case table, failures in detail |
| `.json` | Full summary plus every result row, for programmatic comparison between runs |
| `.csv` | Open in Excel, sort and filter by any dimension |

The run is also written to `app.eval_runs` and `app.eval_results`, which is what
the console's **Evaluation** tab charts — including accuracy trend across runs
once you have more than one.

## Reading the report

The headline table looks like this:

```
| Measure                   | Value  | Basis                                    |
| Completion rate           | 100.0% | 30/30 reached a defined terminal state   |
| Routing accuracy          |  95.0% | 19/20 cases with an expected queue       |
| Priority accuracy         |  87.5% | 7/8 cases with an expected priority      |
| Error rate                |   0.0% | 0/30 unexpected failures                 |
| Full-assertion pass rate  |  93.3% | 28/30 cases passed every assertion       |
| Latency (mean)            | 1420ms | client-observed, end to end              |
```

What to expect, and what each result means:

**Completion rate below 100%** — a real defect. Something timed out or returned
a 500, meaning a path exists that neither succeeds nor fails cleanly. Check the
n8n executions list and `app.audit_log` for ERROR rows at that trace.

**Routing accuracy below 100%** — usually the model, not the rules. The
rule-driven cases (security, privileged access, cost) should be deterministic; if
one of those misses, a rule has a bug. Category-driven cases genuinely vary with
model behaviour. Diagnose with:

```sql
SELECT case_id, expected_queue, actual_queue
FROM app.eval_results
WHERE run_id = 'EV-...' AND queue_pass IS FALSE;
```

**Priority accuracy is the softest measure.** Priority is a judgement call that
two human agents would also disagree on — TC-030 accepts either P3 or P4 for
exactly that reason. Treat sustained drift as a prompt problem, not a single miss.

**Latency p95 much higher than p50** — the tail is the OpenAI call, not your
workflow. Compare `avg_server_ms` against the client mean to see how much is
yours.

**Cases fail with `ai_status: fallback`** — the OpenAI key is missing, out of
credit, or rate-limited. Worth demonstrating deliberately once: the pipeline
still completes, and R8/R9 route the affected requests to Human Review instead of
guessing. Graceful degradation is a feature, and the evaluation shows it working.

## Known limits of this harness

Stated plainly, because a reviewer will think of them:

- **Approval cases are not driven to completion.** TC-006 through TC-009, TC-020
  and TC-023 leave the execution held at the Wait node, and are scored on
  `approval_status: pending`. Testing the full approve/reject cycle needs the
  resume URL, which requires reading the sent mail from the Mailpit API — a
  worthwhile extension, not implemented here. The approval path is demonstrated
  manually instead (setup step 8).
- **A non-deterministic component is being measured with 30 cases.** Small sample,
  variable model. Run it three times before drawing conclusions about a prompt
  change, and quote the range rather than one figure.
- **No load or concurrency testing.** Latency is measured at concurrency 4 on a
  laptop, which says nothing about behaviour at 100 requests per second. Queue
  mode and a load test would be the next step.
- **The external system is a mock.** Its latency and failure profile are
  friendlier than a real ServiceNow instance under load.

## Suggested submission evidence

1. Run the full suite once with a working OpenAI key. Keep the `.md` report.
2. Run it again with the key removed to demonstrate graceful degradation. Keep
   that report too, and note in the video that the difference is intentional.
3. Screenshot the console's Evaluation tab showing both runs and the trend chart.
4. Reference the `.md` report directly in the submission — it is already
   formatted for it.
