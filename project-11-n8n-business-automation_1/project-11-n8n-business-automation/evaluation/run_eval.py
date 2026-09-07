#!/usr/bin/env python3
"""
Evaluation harness for the n8n Business Automation Pipeline (requirement 4.11).

Posts every case in test_cases.json to the live webhook, compares the response
against the expected routing decision, and reports the four measures the
assessment asks for:

    completion rate    - share of cases the pipeline carried to a terminal state
    routing accuracy   - share of cases sent to the expected queue
    priority accuracy  - share of cases given the expected priority
    processing latency - mean / p50 / p95 end-to-end, measured client side
    error rate         - share of cases that failed unexpectedly

Results are written to evaluation/results/ as JSON + CSV + a markdown report,
and (when DATABASE_URL is reachable) inserted into app.eval_runs / eval_results
so the operations console can chart them.

Usage
    python evaluation/run_eval.py
    python evaluation/run_eval.py --kind happy_path --kind security
    python evaluation/run_eval.py --case TC-004 --verbose
    python evaluation/run_eval.py --no-db --concurrency 1
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
CASES_FILE = HERE / "test_cases.json"

DEFAULT_WEBHOOK = os.getenv("EVAL_WEBHOOK_URL", "http://localhost:5678/webhook/it-request")
ITSM_BASE = os.getenv("ITSM_API_BASE", "http://localhost:8000")
ITSM_KEY = os.getenv("ITSM_API_KEY", "local-itsm-key")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://n8n:n8n_local_pw@localhost:5432/n8n"
)

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


# ---------------------------------------------------------------------------
# assertions
# ---------------------------------------------------------------------------
def evaluate_case(case: dict[str, Any], status: int, body: Any) -> dict[str, Any]:
    """Compare one response against the case's expectations.

    Returns a result row.  Each dimension is scored independently so a case
    that routes correctly but mis-prioritises still counts toward routing
    accuracy - averaging them into one pass/fail would hide real behaviour.
    """
    exp = case.get("expect", {})
    checks: list[tuple[str, bool, str]] = []

    payload = body if isinstance(body, dict) else {}

    # --- status code ------------------------------------------------------
    if "http_status_in" in exp:
        ok = status in exp["http_status_in"]
        checks.append(("http_status", ok, f"{status} in {exp['http_status_in']}"))
    elif "http_status" in exp:
        ok = status == exp["http_status"]
        checks.append(("http_status", ok, f"got {status}, want {exp['http_status']}"))

    actual_queue = payload.get("queue")
    actual_priority = payload.get("priority")
    actual_status = payload.get("status")
    rules = payload.get("rules_applied") or []

    # --- queue ------------------------------------------------------------
    queue_pass = None
    if "queue" in exp:
        queue_pass = actual_queue == exp["queue"]
        checks.append(("queue", queue_pass, f"got {actual_queue}, want {exp['queue']}"))

    # --- priority ---------------------------------------------------------
    priority_pass = None
    if "priority" in exp:
        priority_pass = actual_priority == exp["priority"]
        checks.append(("priority", priority_pass,
                       f"got {actual_priority}, want {exp['priority']}"))
    elif "priority_in" in exp:
        priority_pass = actual_priority in exp["priority_in"]
        checks.append(("priority", priority_pass,
                       f"got {actual_priority}, want one of {exp['priority_in']}"))

    # --- approval ---------------------------------------------------------
    if "requires_approval" in exp:
        ok = bool(payload.get("requires_approval")) == exp["requires_approval"]
        checks.append(("requires_approval", ok,
                       f"got {payload.get('requires_approval')}, want {exp['requires_approval']}"))

    if "approval_status" in exp:
        ok = payload.get("approval_status") == exp["approval_status"]
        checks.append(("approval_status", ok,
                       f"got {payload.get('approval_status')}, want {exp['approval_status']}"))

    # The security assertion for the injection cases: whatever else happens, the
    # pipeline must never have *granted* an approval.
    if exp.get("approval_not_granted"):
        got = payload.get("approval_status")
        ok = got != "approved"
        checks.append(("approval_not_granted", ok, f"approval_status is {got}"))

    if "injection_flagged" in exp:
        got = payload.get("injection_suspected")
        ok = bool(got) == exp["injection_flagged"]
        checks.append(("injection_flagged", ok,
                       f"got {got}, want {exp['injection_flagged']}"))

    # --- rules that must have fired --------------------------------------
    for rule in exp.get("rules", []):
        ok = rule in rules
        checks.append((f"rule:{rule}", ok, f"fired={ok}; actual={rules}"))

    # --- sentiment --------------------------------------------------------
    if "sentiment_in" in exp:
        got = payload.get("sentiment") or (payload.get("ai") or {}).get("sentiment")
        ok = got in exp["sentiment_in"]
        checks.append(("sentiment", ok, f"got {got}, want one of {exp['sentiment_in']}"))

    # --- terminal outcome -------------------------------------------------
    outcome_pass = None
    want_outcome = exp.get("outcome")
    if want_outcome == "rejected_validation":
        outcome_pass = status == 400 and bool(payload.get("errors"))
        checks.append(("outcome", outcome_pass, "expected a validation rejection"))
    elif want_outcome == "integration_failed":
        outcome_pass = status == 502 and payload.get("error") == "system_of_record_unavailable"
        checks.append(("outcome", outcome_pass, "expected the integration failure path"))
    elif want_outcome == "retried":
        outcome_pass = status in (200, 502)
        checks.append(("outcome", outcome_pass, "expected recovery or a clean 502"))
    elif want_outcome:
        outcome_pass = actual_status == want_outcome
        checks.append(("outcome", outcome_pass, f"got {actual_status}, want {want_outcome}"))

    failed = [f"{n}: {d}" for n, ok, d in checks if ok is False]

    # "Completed" means the pipeline reached a defined terminal state, which
    # includes a deliberate 400 or 502 - those are designed outcomes, not
    # incompletions.  Anything else (timeout, 500, unparseable) is not.
    completed = status in (200, 400, 502)

    return {
        "case_id": case["case_id"],
        "case_kind": case["kind"],
        "intent": case.get("intent", ""),
        "http_status": status,
        "completed": completed,
        "expected_queue": exp.get("queue"),
        "actual_queue": actual_queue,
        "expected_priority": exp.get("priority") or (exp.get("priority_in") or [None])[0],
        "actual_priority": actual_priority,
        "expected_outcome": want_outcome,
        "actual_outcome": actual_status,
        "queue_pass": queue_pass,
        "priority_pass": priority_pass,
        "outcome_pass": outcome_pass,
        "passed": not failed,
        "failures": failed,
        "rules_applied": rules,
        "ticket_id": payload.get("ticket_id"),
        "external_ref": payload.get("external_ref"),
        "trace_id": payload.get("trace_id"),
        "ai_status": (payload.get("ai") or {}).get("status"),
        "ai_confidence": (payload.get("ai") or {}).get("confidence"),
        "server_processing_ms": payload.get("processing_ms"),
    }


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
def run_case(case: dict[str, Any], webhook: str, timeout: int, verbose: bool) -> dict[str, Any]:
    payload = dict(case["payload"])
    payload["eval_case_id"] = case["case_id"]

    t0 = time.perf_counter()
    status, body, error = 0, None, None
    try:
        r = requests.post(webhook, json=payload, timeout=timeout)
        status = r.status_code
        try:
            body = r.json()
        except ValueError:
            body = {"_raw": r.text[:500]}
    except requests.exceptions.Timeout:
        error = f"client timeout after {timeout}s"
    except requests.exceptions.RequestException as e:
        error = f"{type(e).__name__}: {e}"
    latency_ms = int((time.perf_counter() - t0) * 1000)

    if error:
        row = {
            "case_id": case["case_id"], "case_kind": case["kind"],
            "intent": case.get("intent", ""), "http_status": 0,
            "completed": False, "passed": False, "failures": [error],
            "expected_queue": case.get("expect", {}).get("queue"),
            "actual_queue": None, "expected_priority": None, "actual_priority": None,
            "expected_outcome": case.get("expect", {}).get("outcome"),
            "actual_outcome": None, "queue_pass": None, "priority_pass": None,
            "outcome_pass": None, "rules_applied": [], "ticket_id": None,
            "external_ref": None, "trace_id": None, "ai_status": None,
            "ai_confidence": None, "server_processing_ms": None,
        }
    else:
        row = evaluate_case(case, status, body)

    row["latency_ms"] = latency_ms
    row["error"] = error

    mark = f"{GREEN}PASS{RESET}" if row["passed"] else f"{RED}FAIL{RESET}"
    print(f"  {mark}  {row['case_id']:<8} {row['case_kind']:<15} "
          f"{str(row['http_status']):>3}  {latency_ms:>5} ms  "
          f"{str(row['actual_queue'] or '-'):<16} {str(row['actual_priority'] or '-'):<3}")
    if not row["passed"]:
        for f in row["failures"]:
            print(f"        {RED}-> {f}{RESET}")
    if verbose and body is not None:
        print(f"{DIM}{json.dumps(body, indent=8)[:1200]}{RESET}")

    return row


def summarise(rows: list[dict[str, Any]], model: str) -> dict[str, Any]:
    total = len(rows)
    completed = sum(1 for r in rows if r["completed"])
    errors = sum(1 for r in rows if r["error"] or r["http_status"] in (0, 500))

    routed = [r for r in rows if r["queue_pass"] is not None]
    prios = [r for r in rows if r["priority_pass"] is not None]
    lats = [r["latency_ms"] for r in rows if r["latency_ms"]]
    server_lats = [r["server_processing_ms"] for r in rows if r.get("server_processing_ms")]

    def pct(num, den):
        return round(num / den, 4) if den else 0.0

    def p(vals, q):
        if not vals:
            return 0
        s = sorted(vals)
        idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
        return int(s[idx])

    return {
        "run_id": f"EV-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4].upper()}",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "llm_model": model,
        "total_cases": total,
        "completed_cases": completed,
        "passed_cases": sum(1 for r in rows if r["passed"]),
        "routing_evaluated": len(routed),
        "routing_correct": sum(1 for r in routed if r["queue_pass"]),
        "priority_evaluated": len(prios),
        "priority_correct": sum(1 for r in prios if r["priority_pass"]),
        "error_cases": errors,
        "completion_rate": pct(completed, total),
        "pass_rate": pct(sum(1 for r in rows if r["passed"]), total),
        "routing_accuracy": pct(sum(1 for r in routed if r["queue_pass"]), len(routed)),
        "priority_accuracy": pct(sum(1 for r in prios if r["priority_pass"]), len(prios)),
        "error_rate": pct(errors, total),
        "avg_latency_ms": int(statistics.fmean(lats)) if lats else 0,
        "p50_latency_ms": p(lats, 0.50),
        "p95_latency_ms": p(lats, 0.95),
        "max_latency_ms": max(lats) if lats else 0,
        "avg_server_ms": int(statistics.fmean(server_lats)) if server_lats else 0,
        "by_kind": {
            k: {
                "total": sum(1 for r in rows if r["case_kind"] == k),
                "passed": sum(1 for r in rows if r["case_kind"] == k and r["passed"]),
            }
            for k in sorted({r["case_kind"] for r in rows})
        },
    }


def write_outputs(summary: dict, rows: list[dict]) -> tuple[Path, Path, Path]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    rid = summary["run_id"]

    json_path = RESULTS_DIR / f"{rid}.json"
    json_path.write_text(
        json.dumps({"summary": summary, "results": rows}, indent=2), encoding="utf-8"
    )

    csv_path = RESULTS_DIR / f"{rid}.csv"
    cols = ["case_id", "case_kind", "http_status", "completed", "passed",
            "expected_queue", "actual_queue", "expected_priority", "actual_priority",
            "expected_outcome", "actual_outcome", "latency_ms", "server_processing_ms",
            "ai_status", "ai_confidence", "ticket_id", "external_ref", "trace_id", "error"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    md_path = RESULTS_DIR / f"{rid}.md"
    s = summary
    lines = [
        f"# Evaluation report - {rid}",
        "",
        f"- **Run at:** {s['run_at']}",
        f"- **Model:** {s['llm_model']}",
        f"- **Cases:** {s['total_cases']}",
        "",
        "## Headline measures",
        "",
        "| Measure | Value | Basis |",
        "| --- | --- | --- |",
        f"| Completion rate | **{s['completion_rate']:.1%}** | {s['completed_cases']}/{s['total_cases']} reached a defined terminal state |",
        f"| Routing accuracy | **{s['routing_accuracy']:.1%}** | {s['routing_correct']}/{s['routing_evaluated']} cases with an expected queue |",
        f"| Priority accuracy | **{s['priority_accuracy']:.1%}** | {s['priority_correct']}/{s['priority_evaluated']} cases with an expected priority |",
        f"| Error rate | **{s['error_rate']:.1%}** | {s['error_cases']}/{s['total_cases']} unexpected failures |",
        f"| Full-assertion pass rate | **{s['pass_rate']:.1%}** | {s['passed_cases']}/{s['total_cases']} cases passed every assertion |",
        f"| Latency (mean) | **{s['avg_latency_ms']} ms** | client-observed, end to end |",
        f"| Latency (p50 / p95 / max) | {s['p50_latency_ms']} / {s['p95_latency_ms']} / {s['max_latency_ms']} ms | |",
        f"| Server-side processing (mean) | {s['avg_server_ms']} ms | reported by the workflow |",
        "",
        "## By case category",
        "",
        "| Category | Passed | Total |",
        "| --- | --- | --- |",
    ]
    for k, v in s["by_kind"].items():
        lines.append(f"| {k} | {v['passed']} | {v['total']} |")

    lines += ["", "## Per-case detail", "",
              "| Case | Kind | HTTP | Queue (actual / expected) | Priority | ms | Result |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        q = f"{r['actual_queue'] or '-'} / {r['expected_queue'] or 'n/a'}"
        pr = f"{r['actual_priority'] or '-'} / {r['expected_priority'] or 'n/a'}"
        res = "PASS" if r["passed"] else "FAIL: " + "; ".join(r["failures"])[:160]
        lines.append(f"| {r['case_id']} | {r['case_kind']} | {r['http_status']} | {q} | {pr} | {r['latency_ms']} | {res} |")

    failures = [r for r in rows if not r["passed"]]
    if failures:
        lines += ["", "## Failures in detail", ""]
        for r in failures:
            lines.append(f"### {r['case_id']} - {r['intent']}")
            for f in r["failures"]:
                lines.append(f"- {f}")
            lines.append("")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, csv_path, md_path


def persist_to_db(summary: dict, rows: list[dict]) -> str:
    try:
        import psycopg
    except ImportError:
        return "psycopg not installed - skipped DB write"
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO app.eval_runs
                   (run_id, run_at, llm_model, total_cases, completed_cases, routing_correct,
                    priority_correct, error_cases, completion_rate, routing_accuracy,
                    priority_accuracy, error_rate, avg_latency_ms, p50_latency_ms,
                    p95_latency_ms, notes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (summary["run_id"], summary["run_at"], summary["llm_model"],
                 summary["total_cases"], summary["completed_cases"], summary["routing_correct"],
                 summary["priority_correct"], summary["error_cases"], summary["completion_rate"],
                 summary["routing_accuracy"], summary["priority_accuracy"], summary["error_rate"],
                 summary["avg_latency_ms"], summary["p50_latency_ms"], summary["p95_latency_ms"],
                 f"pass_rate={summary['pass_rate']}"),
            )
            cur.executemany(
                """INSERT INTO app.eval_results
                   (run_id, case_id, case_kind, expected_queue, actual_queue,
                    expected_priority, actual_priority, expected_outcome, actual_outcome,
                    queue_pass, priority_pass, outcome_pass, passed, latency_ms,
                    http_status, trace_id, error)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(summary["run_id"], r["case_id"], r["case_kind"], r["expected_queue"],
                  r["actual_queue"], r["expected_priority"], r["actual_priority"],
                  r["expected_outcome"], r["actual_outcome"], r["queue_pass"],
                  r["priority_pass"], r["outcome_pass"], r["passed"], r["latency_ms"],
                  r["http_status"], r["trace_id"],
                  "; ".join(r["failures"])[:500] or None) for r in rows],
            )
            conn.commit()
        return f"persisted run {summary['run_id']} to app.eval_runs"
    except Exception as e:  # noqa: BLE001 - reporting only, never fatal
        return f"DB write skipped ({type(e).__name__}: {e})"


def reset_itsm() -> str:
    try:
        r = requests.post(f"{ITSM_BASE}/_test/reset",
                          headers={"X-API-Key": ITSM_KEY}, timeout=5)
        return f"ITSM store reset ({r.json().get('discarded', 0)} discarded)"
    except requests.exceptions.RequestException as e:
        return f"ITSM reset skipped ({type(e).__name__})"


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--webhook", default=DEFAULT_WEBHOOK)
    ap.add_argument("--case", action="append", help="run only these case ids")
    ap.add_argument("--kind", action="append", help="run only these case kinds")
    ap.add_argument("--concurrency", type=int, default=int(os.getenv("EVAL_CONCURRENCY", "4")))
    ap.add_argument("--timeout", type=int, default=120, help="per-request timeout in seconds")
    ap.add_argument("--no-db", action="store_true", help="do not write results to Postgres")
    ap.add_argument("--no-reset", action="store_true", help="do not reset the ITSM store first")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    spec = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = spec["cases"]
    if args.case:
        wanted = {c.upper() for c in args.case}
        cases = [c for c in cases if c["case_id"].upper() in wanted]
    if args.kind:
        wanted = {k.lower() for k in args.kind}
        cases = [c for c in cases if c["kind"].lower() in wanted]

    if not cases:
        print(f"{RED}No cases matched the filters.{RESET}")
        return 2

    print(f"\n{'=' * 78}")
    print("  n8n Business Automation Pipeline - evaluation run")
    print(f"  webhook     : {args.webhook}")
    print(f"  cases       : {len(cases)} of {len(spec['cases'])}")
    print(f"  concurrency : {args.concurrency}")
    print(f"{'=' * 78}\n")

    # Approval cases hold the main workflow open while the sub-workflow waits
    # for a manager, so the harness sees them as pending - that is the correct
    # behaviour, and the assertion for those cases checks approval_status only.
    if not args.no_reset:
        print(f"  {DIM}{reset_itsm()}{RESET}\n")

    print(f"  {'':6} {'CASE':<8} {'KIND':<15} {'HTTP':>4}  {'LATENCY':>8}  {'QUEUE':<16} PRI")
    print(f"  {'-' * 72}")

    if args.concurrency <= 1:
        rows = [run_case(c, args.webhook, args.timeout, args.verbose) for c in cases]
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            rows = list(pool.map(
                lambda c: run_case(c, args.webhook, args.timeout, args.verbose), cases))
    rows.sort(key=lambda r: r["case_id"])

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    summary = summarise(rows, model)

    print(f"\n{'=' * 78}")
    print("  RESULTS")
    print(f"{'=' * 78}")
    print(f"  Completion rate    : {summary['completion_rate']:.1%}  "
          f"({summary['completed_cases']}/{summary['total_cases']})")
    print(f"  Routing accuracy   : {summary['routing_accuracy']:.1%}  "
          f"({summary['routing_correct']}/{summary['routing_evaluated']})")
    print(f"  Priority accuracy  : {summary['priority_accuracy']:.1%}  "
          f"({summary['priority_correct']}/{summary['priority_evaluated']})")
    print(f"  Error rate         : {summary['error_rate']:.1%}  "
          f"({summary['error_cases']}/{summary['total_cases']})")
    print(f"  All-assertion pass : {summary['pass_rate']:.1%}  "
          f"({summary['passed_cases']}/{summary['total_cases']})")
    print(f"  Latency mean/p50/p95/max : {summary['avg_latency_ms']} / "
          f"{summary['p50_latency_ms']} / {summary['p95_latency_ms']} / "
          f"{summary['max_latency_ms']} ms")
    print(f"\n  By category:")
    for k, v in summary["by_kind"].items():
        col = GREEN if v["passed"] == v["total"] else YELLOW
        print(f"    {col}{k:<16} {v['passed']}/{v['total']}{RESET}")

    j, c, m = write_outputs(summary, rows)
    print(f"\n  Written:")
    for path in (j, c, m):
        print(f"    {path.relative_to(HERE.parent)}")

    if not args.no_db:
        print(f"  {DIM}{persist_to_db(summary, rows)}{RESET}")

    print()
    return 0 if summary["pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
