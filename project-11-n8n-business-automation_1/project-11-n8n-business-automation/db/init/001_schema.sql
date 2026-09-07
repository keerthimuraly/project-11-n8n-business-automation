-- ============================================================================
-- Project 11 - n8n Business Automation Pipeline
-- Application schema (tickets, audit trail, approvals, evaluation)
--
-- Runs automatically on first `docker compose up` via
-- /docker-entrypoint-initdb.d.  To re-apply on an existing volume:
--    docker compose exec -T postgres psql -U n8n -d n8n < db/init/001_schema.sql
-- ============================================================================

-- n8n keeps its own metadata in a separate schema so application tables and
-- engine tables never collide (DB_POSTGRESDB_SCHEMA=n8n_meta).
CREATE SCHEMA IF NOT EXISTS n8n_meta;
CREATE SCHEMA IF NOT EXISTS app;

SET search_path TO app, public;

-- ---------------------------------------------------------------------------
-- Reference data: queues and SLA policy
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS queues (
    queue_code      TEXT PRIMARY KEY,
    display_name    TEXT        NOT NULL,
    owner_email     TEXT        NOT NULL,
    escalation_email TEXT       NOT NULL,
    description     TEXT
);

CREATE TABLE IF NOT EXISTS sla_policy (
    priority        TEXT PRIMARY KEY,          -- P1..P4
    response_mins   INTEGER     NOT NULL,
    resolve_mins    INTEGER     NOT NULL,
    description     TEXT
);

-- ---------------------------------------------------------------------------
-- Core entity: one row per automated request that entered the pipeline
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id           TEXT PRIMARY KEY,               -- INC-2026-000123
    trace_id            TEXT        NOT NULL,           -- correlates every log row
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    channel             TEXT        NOT NULL DEFAULT 'webhook',

    -- requester context (enriched from the external directory)
    requester_email     TEXT        NOT NULL,
    requester_name      TEXT,
    employee_id         TEXT,
    department          TEXT,
    location            TEXT,
    manager_email       TEXT,
    cost_center         TEXT,
    is_vip              BOOLEAN     NOT NULL DEFAULT FALSE,

    -- raw request
    subject             TEXT        NOT NULL,
    description         TEXT        NOT NULL,
    requested_cost      NUMERIC(12,2) NOT NULL DEFAULT 0,
    affected_asset_tag  TEXT,

    -- AI + rules output
    category            TEXT,
    subcategory         TEXT,
    priority            TEXT,                            -- P1..P4
    urgency             TEXT,
    sentiment           TEXT,
    is_security_incident BOOLEAN    NOT NULL DEFAULT FALSE,
    ai_summary          TEXT,
    entities            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    llm_model           TEXT,
    llm_confidence      NUMERIC(4,3),
    rules_applied       JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- routing + lifecycle
    queue               TEXT        REFERENCES queues(queue_code),
    routing_reason      TEXT,
    assignee_email      TEXT,
    status              TEXT        NOT NULL DEFAULT 'New',
    sla_due_at          TIMESTAMPTZ,
    sla_breached        BOOLEAN     NOT NULL DEFAULT FALSE,
    closed_at           TIMESTAMPTZ,

    -- approval (requirement 4.7)
    requires_approval   BOOLEAN     NOT NULL DEFAULT FALSE,
    approval_status     TEXT        NOT NULL DEFAULT 'not_required',
    approver_email      TEXT,

    -- integration + performance
    external_ref        TEXT,                            -- id returned by ITSM API
    processing_ms       INTEGER,
    error_count         INTEGER     NOT NULL DEFAULT 0,

    CONSTRAINT tickets_priority_chk
        CHECK (priority IS NULL OR priority IN ('P1','P2','P3','P4')),
    CONSTRAINT tickets_status_chk
        CHECK (status IN ('New','Routed','Pending Approval','Approved','Rejected',
                          'In Progress','Escalated','Resolved','Closed','Rejected-Invalid')),
    CONSTRAINT tickets_approval_chk
        CHECK (approval_status IN ('not_required','pending','approved','rejected','expired'))
);

CREATE INDEX IF NOT EXISTS idx_tickets_trace     ON tickets (trace_id);
CREATE INDEX IF NOT EXISTS idx_tickets_queue     ON tickets (queue, status);
CREATE INDEX IF NOT EXISTS idx_tickets_sla       ON tickets (sla_due_at) WHERE status NOT IN ('Resolved','Closed');
CREATE INDEX IF NOT EXISTS idx_tickets_created   ON tickets (created_at DESC);

-- ---------------------------------------------------------------------------
-- Requirement 4.10 - workflow logging / traceability
-- Every meaningful node writes one row here, keyed by trace_id.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    log_id          BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    trace_id        TEXT        NOT NULL,
    ticket_id       TEXT,
    workflow_name   TEXT        NOT NULL,
    execution_id    TEXT,
    node_name       TEXT        NOT NULL,
    event           TEXT        NOT NULL,   -- REQUEST_RECEIVED, VALIDATION_FAILED, ...
    level           TEXT        NOT NULL DEFAULT 'INFO',
    message         TEXT,
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    latency_ms      INTEGER,
    CONSTRAINT audit_level_chk CHECK (level IN ('DEBUG','INFO','WARN','ERROR'))
);

CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_log (trace_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_level ON audit_log (level, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log (event, ts DESC);

-- ---------------------------------------------------------------------------
-- Requirement 4.7 - human-in-the-loop approval tokens
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approvals (
    approval_id     TEXT PRIMARY KEY,
    ticket_id       TEXT        NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    trace_id        TEXT        NOT NULL,
    approver_email  TEXT        NOT NULL,
    token           TEXT        NOT NULL UNIQUE,      -- single-use, in the email link
    reason          TEXT,
    requested_cost  NUMERIC(12,2),
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    decided_at      TIMESTAMPTZ,
    decision        TEXT        NOT NULL DEFAULT 'pending',
    decided_by      TEXT,
    comment         TEXT,
    CONSTRAINT approvals_decision_chk
        CHECK (decision IN ('pending','approved','rejected','expired'))
);

CREATE INDEX IF NOT EXISTS idx_approvals_token  ON approvals (token);
CREATE INDEX IF NOT EXISTS idx_approvals_ticket ON approvals (ticket_id);

-- ---------------------------------------------------------------------------
-- Requirement 4.11 - evaluation runs and per-case results
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id              TEXT PRIMARY KEY,
    run_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    git_ref             TEXT,
    llm_model           TEXT,
    total_cases         INTEGER NOT NULL,
    completed_cases     INTEGER NOT NULL,
    routing_correct     INTEGER NOT NULL,
    priority_correct    INTEGER NOT NULL,
    error_cases         INTEGER NOT NULL,
    completion_rate     NUMERIC(5,4),
    routing_accuracy    NUMERIC(5,4),
    priority_accuracy   NUMERIC(5,4),
    error_rate          NUMERIC(5,4),
    avg_latency_ms      INTEGER,
    p50_latency_ms      INTEGER,
    p95_latency_ms      INTEGER,
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS eval_results (
    result_id           BIGSERIAL PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES eval_runs(run_id) ON DELETE CASCADE,
    case_id             TEXT NOT NULL,
    case_kind           TEXT NOT NULL,          -- happy_path | edge | negative | security
    expected_queue      TEXT,
    actual_queue        TEXT,
    expected_priority   TEXT,
    actual_priority     TEXT,
    expected_outcome    TEXT,
    actual_outcome      TEXT,
    queue_pass          BOOLEAN,
    priority_pass       BOOLEAN,
    outcome_pass        BOOLEAN,
    passed              BOOLEAN,
    latency_ms          INTEGER,
    http_status         INTEGER,
    trace_id            TEXT,
    error               TEXT
);

CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results (run_id);

-- ---------------------------------------------------------------------------
-- Convenience views used by the operations console
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_queue_load AS
SELECT queue,
       count(*)                                            AS total,
       count(*) FILTER (WHERE status = 'Pending Approval')  AS pending_approval,
       count(*) FILTER (WHERE priority = 'P1')              AS p1,
       count(*) FILTER (WHERE sla_breached)                 AS sla_breached,
       round(avg(processing_ms))                            AS avg_processing_ms
FROM tickets
GROUP BY queue
ORDER BY total DESC;

CREATE OR REPLACE VIEW v_ticket_trace AS
SELECT t.ticket_id,
       t.trace_id,
       t.subject,
       t.category,
       t.priority,
       t.queue,
       t.status,
       t.approval_status,
       t.processing_ms,
       count(a.log_id)                                  AS log_events,
       count(a.log_id) FILTER (WHERE a.level = 'ERROR') AS error_events
FROM tickets t
LEFT JOIN audit_log a ON a.trace_id = t.trace_id
GROUP BY t.ticket_id, t.trace_id, t.subject, t.category,
         t.priority, t.queue, t.status, t.approval_status, t.processing_ms;

-- Auto-maintain updated_at
CREATE OR REPLACE FUNCTION app.touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tickets_touch ON tickets;
CREATE TRIGGER trg_tickets_touch
    BEFORE UPDATE ON tickets
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

-- Make `app` the default schema for the n8n Postgres credential and psql.
ALTER DATABASE n8n SET search_path TO app, public;
