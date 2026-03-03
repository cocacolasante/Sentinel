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
                            CHECK (status IN ('pending', 'in_progress', 'done', 'cancelled')),
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

-- ── Brain settings (key-value) ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS brain_settings (
    key         TEXT        PRIMARY KEY,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
