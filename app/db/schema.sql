-- ============================================================
-- AI Brain — Phase 2 Database Schema
-- Run once on first boot via init_schema() in postgres.py
-- ============================================================

-- Long-term conversation storage (beyond Redis 4hr TTL)
CREATE TABLE IF NOT EXISTS conversations (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT        NOT NULL,
    role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT        NOT NULL,
    task_type   TEXT,
    intent      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations (session_id, created_at DESC);

-- Task tracking
CREATE TABLE IF NOT EXISTS tasks (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT        NOT NULL,
    description TEXT,
    status      TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'in_progress', 'done', 'cancelled', 'failed', 'archived')),
    priority    TEXT        NOT NULL DEFAULT 'normal'
                            CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    due_date    TIMESTAMPTZ,
    source      TEXT,       -- 'slack', 'gmail', 'github', 'manual'
    external_id TEXT,       -- e.g. GitHub issue number
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Calendar event cache (synced from Google Calendar)
CREATE TABLE IF NOT EXISTS calendar_events (
    id          TEXT        PRIMARY KEY,   -- Google Calendar event ID
    title       TEXT        NOT NULL,
    description TEXT,
    location    TEXT,
    start_time  TIMESTAMPTZ NOT NULL,
    end_time    TIMESTAMPTZ NOT NULL,
    calendar_id TEXT        NOT NULL DEFAULT 'primary',
    synced_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_calendar_start ON calendar_events (start_time);

-- OAuth tokens (Google — stores refresh token for server-side auth)
CREATE TABLE IF NOT EXISTS oauth_tokens (
    provider      TEXT        PRIMARY KEY,
    access_token  TEXT,
    refresh_token TEXT        NOT NULL,
    token_expiry  TIMESTAMPTZ,
    scopes        TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Contacts
CREATE TABLE IF NOT EXISTS contacts (
    id          BIGSERIAL   PRIMARY KEY,
    name        TEXT        NOT NULL,
    email       TEXT,
    phone       TEXT,        -- E.164 format, e.g. +12125551234
    whatsapp    TEXT,        -- WhatsApp number, defaults to phone if blank
    company     TEXT,
    github      TEXT,
    slack_id    TEXT,
    tags        TEXT,        -- comma-separated tags
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_contacts_email  ON contacts (email);
CREATE INDEX IF NOT EXISTS idx_contacts_github ON contacts (github);
CREATE INDEX IF NOT EXISTS idx_contacts_name   ON contacts (lower(name) text_pattern_ops);

-- Key-value user profile store
CREATE TABLE IF NOT EXISTS user_profile (
    key         TEXT        PRIMARY KEY,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── RMM: Managed devices (MeshCentral inventory) ────────────────────────────────
CREATE TABLE IF NOT EXISTS rmm_devices (
    node_id         TEXT        PRIMARY KEY,
    name            TEXT        NOT NULL,
    hostname        TEXT,
    ip_address      TEXT,
    os_name         TEXT,
    agent_version   TEXT,
    mesh_id         TEXT,
    group_name      TEXT,       -- production | staging | dev
    project         TEXT,       -- sentinel | language-tutor | n8n | etc.
    is_online       BOOLEAN     NOT NULL DEFAULT FALSE,
    cpu_usage       FLOAT,
    memory_usage    FLOAT,
    disk_usage      FLOAT,
    uptime_seconds  BIGINT,
    last_seen       TIMESTAMPTZ,
    tags            JSONB       NOT NULL DEFAULT '[]',
    metadata        JSONB       NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rmm_devices_online  ON rmm_devices (is_online);
CREATE INDEX IF NOT EXISTS idx_rmm_devices_group   ON rmm_devices (group_name);
CREATE INDEX IF NOT EXISTS idx_rmm_devices_project ON rmm_devices (project);

-- ── RMM: Event log (agent events, alerts, command results) ───────────────────
CREATE TABLE IF NOT EXISTS rmm_events (
    id              BIGSERIAL   PRIMARY KEY,
    node_id         TEXT,
    event_type      TEXT        NOT NULL,
    severity        TEXT        NOT NULL DEFAULT 'info'
                                CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info')),
    hostname        TEXT,
    project         TEXT,
    group_name      TEXT,
    details         JSONB       NOT NULL DEFAULT '{}',
    correlation_id  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rmm_events_node     ON rmm_events (node_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rmm_events_type     ON rmm_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rmm_events_severity ON rmm_events (severity, created_at DESC);

-- ── PAI: Session summaries (warm memory tier) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS session_summaries (
    id          BIGSERIAL   PRIMARY KEY,
    session_id  TEXT        NOT NULL,
    summary     TEXT        NOT NULL,
    turn_count  INT         NOT NULL DEFAULT 0,
    intent_mix  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_session_summaries_session ON session_summaries (session_id, created_at DESC);

-- ── PAI: Qdrant cross-reference (cold memory tier) ────────────────────────────
CREATE TABLE IF NOT EXISTS interaction_embeddings (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id   TEXT        NOT NULL,
    qdrant_id    TEXT        NOT NULL,
    content_hash TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_embeddings_session ON interaction_embeddings (session_id);

-- ── PAI: Interaction ratings (learning tier) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS interaction_ratings (
    id             BIGSERIAL   PRIMARY KEY,
    session_id     TEXT        NOT NULL,
    message_index  INT         NOT NULL DEFAULT 0,
    rating         SMALLINT    NOT NULL CHECK (rating BETWEEN 1 AND 10),
    comment        TEXT,
    intent         TEXT,
    source         TEXT        DEFAULT 'api',
    qdrant_stored  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ratings_session ON interaction_ratings (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ratings_intent  ON interaction_ratings (intent, rating DESC);

-- ── Eval system: per-test results (agent quality evals) ───────────────────────
CREATE TABLE IF NOT EXISTS eval_results (
    id          BIGSERIAL   PRIMARY KEY,
    run_id      TEXT        NOT NULL,
    agent_name  TEXT        NOT NULL,
    test_name   TEXT        NOT NULL,
    score       NUMERIC(4,2) NOT NULL,
    passed      BOOLEAN     NOT NULL,
    threshold   INT         NOT NULL DEFAULT 7,
    reasoning   TEXT,
    latency_ms  NUMERIC(10,1),
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_eval_results_agent   ON eval_results (agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_results_run     ON eval_results (run_id);

-- ── Eval system: nightly integration reliability checks ───────────────────────
CREATE TABLE IF NOT EXISTS integration_eval_results (
    id           BIGSERIAL   PRIMARY KEY,
    integration  TEXT        NOT NULL,
    passed       BOOLEAN     NOT NULL,
    latency_ms   NUMERIC(10,1),
    error_message TEXT,
    checked_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_integration_eval_integration ON integration_eval_results (integration, checked_at DESC);

-- ── Approval system: write-action audit trail ──────────────────────────────────
CREATE TABLE IF NOT EXISTS pending_write_tasks (
    task_id     TEXT        PRIMARY KEY,
    session_id  TEXT        NOT NULL,
    action      TEXT        NOT NULL,
    title       TEXT,
    params      JSONB       DEFAULT '{}',
    category    TEXT        NOT NULL DEFAULT 'standard',
    status      TEXT        NOT NULL DEFAULT 'awaiting_approval'
                            CHECK (status IN ('awaiting_approval','executing','completed','cancelled','failed')),
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_write_tasks_status  ON pending_write_tasks (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_write_tasks_session ON pending_write_tasks (session_id, created_at DESC);

-- ── Sentry issue audit log ──────────────────────────────────────────────────────
-- Populated by the Sentry webhook router. Drives pending_write_tasks creation.
CREATE TABLE IF NOT EXISTS sentry_issues (
    issue_id    TEXT        PRIMARY KEY,   -- Sentry issue ID
    title       TEXT        NOT NULL,
    level       TEXT        NOT NULL,      -- fatal | critical | error | warning | info | debug
    status      TEXT        NOT NULL,      -- unresolved | resolved | ignored
    project     TEXT,
    permalink   TEXT,
    count       INTEGER     NOT NULL DEFAULT 0,
    platform    TEXT,
    first_seen  TEXT,
    category    TEXT        NOT NULL,      -- breaking | critical | standard | none
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sentry_issues_received ON sentry_issues (received_at DESC);
CREATE INDEX IF NOT EXISTS idx_sentry_issues_level    ON sentry_issues (level, received_at DESC);

-- ── Brain settings (key-value) ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS brain_settings (
    key         TEXT        PRIMARY KEY,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Task board: extended columns (added after initial schema) ─────────────────
-- priority_num:   numeric 1–5 (1=low, 5=critical) — mirrors text priority field
-- approval_level: 1=auto-approve, 2=needs review, 3=requires sign-off
-- assigned_to:    free-text assignee name / email
-- tags:           comma-separated labels
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority_num    SMALLINT    DEFAULT 3 CHECK (priority_num BETWEEN 1 AND 5);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS approval_level  SMALLINT    DEFAULT 2 CHECK (approval_level BETWEEN 1 AND 3);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assigned_to     TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tags            TEXT;

-- ── Task board: background execution columns ─────────────────────────────────
-- commands:        ordered list of shell commands to execute (JSONB array of strings)
-- execution_queue: 'tasks_workspace' (serialised, 1 at a time) or 'tasks_general' (parallel, 3 at a time)
-- celery_task_id:  Celery task UUID for Flower tracking
-- slack_channel:   Slack channel ID to post results back to
-- slack_thread_ts: Slack thread timestamp (message to reply into)
-- session_id:      originating session (fallback Slack context lookup)
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS commands         JSONB       DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS execution_queue  TEXT        DEFAULT 'tasks_general';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS celery_task_id   TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS slack_channel    TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS slack_thread_ts  TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS session_id       TEXT;
-- sentinel-tasks channel thread anchor (separate from the originating Slack thread)
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_slack_ts    TEXT;

-- ── Task board: dependency tracking ───────────────────────────────────────────
-- blocked_by: list of task IDs that must reach status='done' before this task runs.
--             Stored as JSONB array of integers: e.g. [3, 7]
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS blocked_by JSONB DEFAULT '[]';

-- ── AI Milestone log ──────────────────────────────────────────────────────────
-- Every confirmed write action the AI executes is recorded here and posted to
-- #sentinel-milestones in Slack.
CREATE TABLE IF NOT EXISTS ai_milestones (
    id           BIGSERIAL   PRIMARY KEY,
    session_id   TEXT        NOT NULL,
    action       TEXT        NOT NULL,
    intent       TEXT,
    summary      TEXT,
    detail       JSONB       DEFAULT '{}',
    agent        TEXT,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_milestones_session ON ai_milestones (session_id, triggered_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_milestones_action  ON ai_milestones (action, triggered_at DESC);
