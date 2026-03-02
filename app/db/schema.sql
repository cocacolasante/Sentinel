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
    github      TEXT,
    slack_id    TEXT,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_contacts_email  ON contacts (email);
CREATE INDEX IF NOT EXISTS idx_contacts_github ON contacts (github);

-- Key-value user profile store
CREATE TABLE IF NOT EXISTS user_profile (
    key         TEXT        PRIMARY KEY,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
