# AI Brain — CSuite Code

Personalized AI assistant for Anthony. Runs on an Ubuntu server as a FastAPI/Python application. Slack is the primary interface; a REST API is also available.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Prerequisites](#prerequisites)
3. [Environment Variables](#environment-variables)
4. [Deployment](#deployment)
5. [Managing the Deployment](#managing-the-deployment)
6. [Dashboards](#dashboards)
7. [TELOS — Personal Context](#telos--personal-context)
8. [Slack Interface](#slack-interface)
9. [REST API](#rest-api)
10. [Intent Routing & Agent Personas](#intent-routing--agent-personas)
11. [Cost Tracking & Rate Limiting](#cost-tracking--rate-limiting)
12. [Memory System](#memory-system)
13. [Task Queue — Celery + Flower](#task-queue--celery--flower)
14. [Observability](#observability)
15. [Eval System](#eval-system)
16. [Security](#security)
17. [Google OAuth Setup](#google-oauth-setup)
18. [Updating](#updating)
19. [Troubleshooting](#troubleshooting)

---

## Architecture

```
Slack / REST API
       │
       ▼
  RateLimiter  ──── per-session request throttle (Redis sliding window)
       │
  SecurityHook ──── blocks prompt injection attempts
       │
  Dispatcher
  ├── MemoryManager
  │    ├── Redis      (hot — current session, 4hr TTL, last 20 turns)
  │    ├── Postgres   (warm — Haiku-generated session summaries)
  │    └── Qdrant     (cold — semantic embeddings of high-signal turns)
  ├── IntentClassifier  (Haiku — classifies message → intent + params)
  ├── AgentRegistry     (selects persona based on intent + keywords)
  ├── SkillRegistry     (executes the matching skill, returns context data)
  └── LLMRouter
       ├── CostTracker.check_budget() ← blocks if daily ceiling hit
       ├── client.messages.create()   ← Anthropic API call
       └── CostTracker.record()       ← atomic Redis counters + Slack alert
       │
  LoggingHook  ──── Loguru structured log + WebSocket event bus emit
       │
  DispatchResult (reply, intent, agent, session_id)
```

**All containers (Docker Compose):**

| Container | Role | Internal port |
|-----------|------|---------------|
| `ai-brain` | FastAPI application | 8000 |
| `ai-postgres` | Warm memory, ratings, eval results | 5432 |
| `ai-redis` | Hot session memory (DB 0), Celery broker (DB 1), backend (DB 2) | 6379 |
| `ai-qdrant` | Vector / cold memory | 6333 |
| `ai-n8n` | Workflow automation (action executor only) | 5678 |
| `ai-nginx` | Reverse proxy + SSL termination | 80, 443 |
| `ai-celery-worker` | Background task executor (evals queue) | — |
| `ai-celery-beat` | Cron scheduler (weekly evals, nightly checks) | — |
| `ai-flower` | Celery task monitoring UI | 5555 |
| `ai-redis-exporter` | Exports Redis metrics to Prometheus | 9121 |
| `ai-celery-exporter` | Exports Celery metrics to Prometheus | 9808 |
| `ai-prometheus` | Metrics scraper and time-series storage | 9090 |
| `ai-grafana` | Metrics dashboards | 3000 |

---

## Prerequisites

- Ubuntu 22.04 server (2 vCPU / 4 GB RAM recommended — Grafana + Prometheus add ~300 MB)
- A domain name pointing to the server's IP
- Anthropic API key (required)
- Optional: OpenAI API key (semantic embeddings — falls back to zero vectors without it), Google OAuth credentials, GitHub token, Slack app credentials, Sentry DSN

---

## Environment Variables

Copy the example and fill in your values:

```bash
cd ~/ai-brain
cp .env.example .env
nano .env
```

Full reference — every variable the brain reads:

```bash
# ── Core ──────────────────────────────────────────────────────────────────────
SECRET_KEY=84b760f1f9c146b0918d52be49785566b2e5c64b54fdc47d30e6692079215a71
ENVIRONMENT=production           # development | production

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
POSTGRES_USER=brain
POSTGRES_PASSWORD=Cola1994
POSTGRES_DB=aibrain

# ── Redis ──────────────────────────────────────────────────────────────────────
REDIS_PASSWORD=Cola1994
# REDIS_HOST and REDIS_PORT default to redis:6379 inside Docker

# ── LLM APIs ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY=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
OPENAI_API_KEY=sk-...            # optional — enables real Qdrant embeddings

# ── Slack ──────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN=xoxb-...         # Bot token for posting messages
SLACK_SIGNING_SECRET=...         # For request verification
SLACK_APP_TOKEN=xapp-...         # Socket Mode token (starts with xapp-)
SLACK_EVAL_CHANNEL=brain-evals   # Channel for weekly eval scorecards
SLACK_ALERT_CHANNEL=brain-alerts # Channel for budget threshold alerts

# ── n8n ────────────────────────────────────────────────────────────────────────
N8N_HOST=csuitecoden8n.com
N8N_USER=Colasante
N8N_PASSWORD=Sonicajc1994$
N8N_WEBHOOK_URL=http://n8n:5678

# ── Domain ─────────────────────────────────────────────────────────────────────
DOMAIN=your-domain.com

# ── Google (Gmail + Calendar) ──────────────────────────────────────────────────
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=...         # run: python3 scripts/google_auth.py
GOOGLE_CALENDAR_ID=primary

# ── GitHub ─────────────────────────────────────────────────────────────────────
GITHUB_TOKEN=ghp_...
GITHUB_USERNAME=your-username
GITHUB_DEFAULT_REPO=your-username/your-repo

# ── Home Assistant ─────────────────────────────────────────────────────────────
HOME_ASSISTANT_URL=http://192.168.1.100:8123
HOME_ASSISTANT_TOKEN=...

# ── TELOS personal context ─────────────────────────────────────────────────────
TELOS_DIR=/home/ubuntu/ai-brain/telos
TELOS_CACHE_TTL_SECONDS=300      # how often to re-read telos/ files (seconds)

# ── Observability ──────────────────────────────────────────────────────────────
SENTRY_DSN=https://key@sentry.io/project  # leave blank to disable
LOG_LEVEL=INFO                             # DEBUG for verbose output
LOG_DIR=/var/log/aibrain

# ── Cost tracking & rate limiting ──────────────────────────────────────────────
DAILY_COST_CEILING_USD=10.0      # hard block at this amount; 0 = disabled
BUDGET_ALERT_THRESHOLDS=0.5,0.8,1.0  # Slack alerts at 50%, 80%, 100%
SONNET_DAILY_TOKEN_BUDGET=0      # per-model token cap; 0 = unlimited
HAIKU_DAILY_TOKEN_BUDGET=0
RATE_LIMIT_PER_MINUTE=20         # max requests per session per minute
RATE_LIMIT_PER_HOUR=200          # max requests per session per hour

# ── Memory ─────────────────────────────────────────────────────────────────────
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
QDRANT_COLLECTION=brain_memories
MEMORY_FLUSH_INTERVAL_TURNS=10   # flush Postgres summary every N turns

# ── Monitoring dashboards ──────────────────────────────────────────────────────
GRAFANA_USER=admin
GRAFANA_PASSWORD=<strong password>
FLOWER_USER=admin
FLOWER_PASSWORD=<strong password>
```

---

## Deployment

### 1. Bootstrap the server (run once)

```bash
chmod +x scripts/server_setup.sh
./scripts/server_setup.sh
```

Installs Docker + Docker Compose, configures UFW (22/80/443), enables Fail2ban. Log out and back in after so Docker group membership takes effect.

### 2. Copy project files

```bash
# From your local machine
scp -r . user@your-server:~/ai-brain/
```

### 3. Configure environment

Fill in `.env` using the reference above.

### 4. Configure Nginx

Edit `nginx/nginx.conf` and replace `YOUR_DOMAIN` with your actual domain:

```nginx
server_name your-domain.com;
```

### 5. Get SSL certificates

```bash
chmod +x scripts/get_ssl.sh
./scripts/get_ssl.sh
```

### 6. Deploy

```bash
./scripts/deploy.sh
```

This builds the brain container and starts all 13 services. Verify:

```bash
curl https://your-domain.com/
# → {"status":"Brain is alive","version":"2.0.0"}

# Confirm all containers are up
docker compose ps
```

All 13 containers should show `running` or `healthy`. The Celery worker and beat may take 10–15 seconds to connect to Redis after startup.

---

## Managing the Deployment

```bash
# Rebuild and restart everything
./scripts/deploy.sh

# Restart without rebuild
./scripts/deploy.sh restart

# Tail all logs
./scripts/deploy.sh logs

# Tail a specific container
docker compose logs -f brain
docker compose logs -f celery-worker
docker compose logs -f grafana

# Show container status and health
./scripts/deploy.sh status

# Stop everything
./scripts/deploy.sh stop

# Rebuild only the brain after a code change
docker compose build --no-cache brain && docker compose up -d brain

# Rebuild Celery worker after a task change
docker compose build --no-cache celery-worker && \
  docker compose up -d celery-worker celery-beat flower
```

---

## Dashboards

Three monitoring UIs are available after deployment.

### Grafana — Infrastructure Metrics

**URL:** `https://your-domain.com/grafana/`

**Login:** `GRAFANA_USER` / `GRAFANA_PASSWORD` from `.env`

#### First-time setup

1. Open `https://your-domain.com/grafana/` in your browser
2. Log in with your admin credentials
3. The **Brain Overview** dashboard loads automatically as the home dashboard
4. If it doesn't appear, navigate to: **Dashboards → Browse → Brain Overview**

#### What you'll see on the Brain Overview dashboard

The dashboard has 16 panels arranged in rows:

| Row | Panels |
|-----|--------|
| Stats (top) | Request rate · Error rate · P95 latency · Celery workers online |
| Traffic | Request rate + errors over time · Response latency percentiles (p50/p95/p99) |
| LLM | Tokens per minute by model · LLM API latency by model |
| Celery | Requests by intent (last 1h) · Celery tasks by state over time · Queue depth |
| Redis | Redis hit rate over time · Redis connected clients |
| Cost | Daily spend vs ceiling · Daily cost accumulation with ceiling line |

#### Navigating Grafana

```
Top bar:  time range picker (top right) — try "Last 1 hour", "Last 6 hours", "Today"
          auto-refresh selector (clock icon) — set to 30s for live monitoring

Panel:    click the panel title → "Explore" to run ad hoc PromQL queries
          click a legend item in a time series to isolate that series

Explore:  Dashboards → Explore (left sidebar compass icon)
          type any PromQL query and see raw data
```

#### Useful PromQL queries to run in Explore

```promql
# P95 end-to-end response latency in milliseconds
histogram_quantile(0.95, sum by(le) (rate(brain_response_latency_seconds_bucket[5m]))) * 1000

# LLM token consumption rate (tokens per minute, by model)
sum by(model, direction) (rate(brain_llm_tokens_total[1m]) * 60)

# Today's total API spend in USD
brain_cost_usd_daily

# Error rate percentage
100 * sum(rate(brain_requests_total{success="false"}[5m]))
    / sum(rate(brain_requests_total[5m]))

# Celery task failure rate
rate(celery_tasks_total{state="FAILURE"}[5m]) / rate(celery_tasks_total[5m])

# Redis memory
redis_memory_used_bytes

# Requests broken down by intent (last hour)
sum by(intent) (increase(brain_requests_total[1h]))
```

#### Creating your own panels

1. Click **+** (top right) → **New dashboard** — or open Brain Overview and click **Edit**
2. **Add panel** → select metric type → paste a PromQL query
3. Click **Save dashboard** — changes persist in the `grafana-data` Docker volume

---

### Flower — Celery Task Monitor

**URL:** `https://your-domain.com/flower/`

**Login:** `FLOWER_USER` / `FLOWER_PASSWORD` from `.env` (HTTP Basic Auth prompt)

#### What you'll see

| Tab | What it shows |
|-----|---------------|
| **Dashboard** | Active workers, processed task count, failed count, online/offline status |
| **Tasks** | Full history of every task run — name, state, runtime, args, result or traceback |
| **Workers** | Per-worker concurrency, queues, active tasks |
| **Broker** | Queue names and depths (how many tasks are waiting) |

#### Common things to do in Flower

**Check if scheduled jobs ran:**
1. Click **Tasks** tab
2. Filter by task name: `app.worker.tasks.run_weekly_agent_evals`
3. Look for `SUCCESS` state and the timestamp — confirms the Sunday job fired

**Check queue depth:**
1. Click **Broker** tab
2. The `evals` queue should normally be 0 (empty between runs)
3. A non-zero count means a task is queued but hasn't started yet

**Inspect a failed task:**
1. Click **Tasks** tab → find a row with state `FAILURE`
2. Click the task UUID
3. The traceback shows exactly where it failed

**Trigger a task manually without the CLI:**
- Use the REST API that Flower exposes: `POST /api/task/async-apply/app.worker.tasks.run_weekly_agent_evals`
- Or use the CLI (see [Task Queue section](#task-queue--celery--flower) below)

---

### Prometheus — Raw Metrics

Prometheus is internal-only (not exposed through Nginx). Access it via SSH tunnel:

```bash
# On your local machine — tunnel port 9090
ssh -L 9090:localhost:9090 user@your-server

# Then open in browser
open http://localhost:9090
```

Use Prometheus directly to:
- Check scrape health: **Status → Targets** — all four targets should show `UP`
- Run one-off PromQL queries in the **Graph** tab
- Inspect raw metric values: **Status → TSDB Status**

If any target shows `DOWN`, check:
- `brain`: is `ai-brain` container healthy? — `docker compose logs brain`
- `redis`: is `ai-redis-exporter` running? — `docker compose logs redis-exporter`
- `celery`: is `ai-celery-exporter` running? — `docker compose logs celery-exporter`

---

## TELOS — Personal Context

The `telos/` directory holds Markdown files injected into every LLM system prompt. Edit them to keep the brain's context current:

```
telos/
├── mission.md      # what you're building and why
├── goals.md        # current targets and metrics
├── projects.md     # active projects and status
├── beliefs.md      # core principles
├── strategies.md   # how you operate
├── style.md        # communication preferences
└── context.md      # personal details, stack, tools
```

After editing, reload without restarting:

```bash
curl -X POST https://your-domain.com/api/v1/telos/reload
# → {"reloaded": ["beliefs.md", "context.md", "goals.md", ...]}
```

The cache also auto-refreshes every 5 minutes (`TELOS_CACHE_TTL_SECONDS`).

---

## Slack Interface

The brain connects via Socket Mode — no public webhook required.

**Sending messages:**
- DM the bot directly, or
- `@Brain` mention it in any channel

**Examples:**

```
What should I focus on this week?
Debug this Python function: [paste code]
Draft an email to sarah@company.com about the Q1 proposal
What do I have tomorrow?
What are my open GitHub issues in ai-brain?
Turn off the living room lights
Write an Instagram caption for my new product launch
confirm      ← executes a pending write action
cancel       ← aborts a pending write action
```

Write actions (send email, create calendar event) ask for `confirm` before executing.

---

## REST API

Base URL: `https://your-domain.com`

Interactive docs (non-production only): `https://your-domain.com/docs`

### Full endpoint reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health probe — returns `{"status":"Brain is alive"}` |
| `GET` | `/api/v1/health` | Detailed health check (Redis + Postgres) |
| `POST` | `/api/v1/chat` | Send a message, get a reply |
| `DELETE` | `/api/v1/chat/{session_id}` | Clear session history |
| `GET` | `/api/v1/agents` | List all registered agent personas |
| `POST` | `/api/v1/telos/reload` | Reload TELOS context files from disk |
| `GET` | `/api/v1/integrations/status` | Which integrations are configured |
| `GET` | `/api/v1/integrations/gmail` | List emails |
| `GET` | `/api/v1/integrations/calendar` | List calendar events |
| `GET` | `/api/v1/integrations/github/issues` | List GitHub issues |
| `GET` | `/api/v1/integrations/github/notifications` | GitHub notifications |
| `GET` | `/api/v1/integrations/github/prs` | Open pull requests |
| `GET` | `/api/v1/integrations/home-assistant/states` | All HA entity states |
| `GET` | `/api/v1/integrations/home-assistant/entity/{id}` | Single HA entity |
| `POST` | `/api/v1/integrations/home-assistant/service` | Call an HA service |
| `POST` | `/api/v1/integrations/n8n/trigger` | Trigger an n8n workflow |
| `POST` | `/api/v1/feedback/rate` | Rate a response (1–10) |
| `POST` | `/api/v1/feedback/thumbs` | Thumbs up / down |
| `GET` | `/api/v1/feedback/summary` | Aggregate rating stats |
| `GET` | `/api/v1/costs` | Today's LLM spend and remaining budget |
| `WS` | `/api/v1/observe/stream` | Real-time event stream (WebSocket) |
| `GET` | `/api/v1/observe/metrics` | Aggregate performance metrics |
| `GET` | `/api/v1/observe/events` | Recent event buffer |
| `GET` | `/metrics` | Prometheus scrape endpoint (internal) |

### Chat

```bash
curl -X POST https://your-domain.com/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What are my open GitHub PRs?", "session_id": "my-session"}'

# Response
{
  "reply": "...",
  "session_id": "my-session",
  "intent": "github_read",
  "agent": "engineer"
}

# Clear a session's history
curl -X DELETE https://your-domain.com/api/v1/chat/my-session
```

### Agents

```bash
curl https://your-domain.com/api/v1/agents

# Response
{
  "agents": [
    {"name": "engineer",   "preferred_model": "claude-sonnet-4-6", "max_tokens": 8096},
    {"name": "writer",     "preferred_model": "claude-sonnet-4-6", "max_tokens": 4096},
    {"name": "researcher", "preferred_model": "claude-sonnet-4-6", "max_tokens": 4096},
    {"name": "strategist", "preferred_model": "claude-sonnet-4-6", "max_tokens": 4096},
    {"name": "marketing",  "preferred_model": "claude-sonnet-4-6", "max_tokens": 6000},
    {"name": "default",    "preferred_model": "claude-sonnet-4-6", "max_tokens": 2048}
  ]
}
```

### Feedback / Ratings

Ratings ≥ 8 are automatically stored in Qdrant and surfaced in future relevant conversations.

```bash
# Rate a response 1–10
curl -X POST https://your-domain.com/api/v1/feedback/rate \
  -H "Content-Type: application/json" \
  -d '{"session_id": "my-session", "message_index": 0, "rating": 9, "intent": "code"}'

# Thumbs up
curl -X POST https://your-domain.com/api/v1/feedback/thumbs \
  -H "Content-Type: application/json" \
  -d '{"session_id": "my-session", "message_index": 0, "positive": true}'

# Summary stats
curl https://your-domain.com/api/v1/feedback/summary
# → {"total_ratings": 47, "avg_rating": 8.3, "top_intent": "code"}
```

### Cost tracking

```bash
curl https://your-domain.com/api/v1/costs

# Response
{
  "date": "2026-03-01",
  "total_cost_usd": 0.142300,
  "daily_ceiling_usd": 10.0,
  "pct_of_ceiling": 1.4,
  "remaining_usd": 9.857700,
  "rate_limits": {"per_minute": 20, "per_hour": 200},
  "models": {
    "claude-sonnet-4-6": {
      "tokens_in": 42000, "tokens_out": 6800, "tokens_total": 48800,
      "cost_usd": 0.228000, "token_budget": null, "pct_of_budget": null
    },
    "claude-haiku-4-5-20251001": {
      "tokens_in": 12000, "tokens_out": 3000, "tokens_total": 15000,
      "cost_usd": 0.006750, "token_budget": null, "pct_of_budget": null
    }
  }
}
```

### Health

```bash
curl https://your-domain.com/api/v1/health
# → {"status": "ok", "redis": true, "postgres": true}
```

---

## Intent Routing & Agent Personas

Every message is classified by Claude Haiku into an intent. The intent selects both the skill to execute and the agent persona to respond as.

### Intent → Skill → Agent mapping

| Intent | Skill | Default Agent |
|--------|-------|---------------|
| `chat` | ChatSkill (no external call) | Default |
| `code` | CodeSkill (routing hint) | Engineer |
| `research` | ResearchSkill (Qdrant semantic search) | Researcher |
| `reasoning` | ChatSkill | Strategist |
| `writing` | ChatSkill | Writer |
| `gmail_read` | GmailReadSkill | Default |
| `gmail_send` | GmailSendSkill (**confirmation required**) | Writer |
| `calendar_read` | CalendarReadSkill | Default |
| `calendar_write` | CalendarWriteSkill (**confirmation required**) | Default |
| `github_read` | GitHubReadSkill | Engineer |
| `github_write` | GitHubWriteSkill (**confirmation required**) | Engineer |
| `smart_home` | SmartHomeSkill | Default |
| `n8n_execute` | N8nSkill | Default |
| `content_draft` | ContentDraftSkill | Marketing |
| `social_caption` | SocialCaptionSkill | Marketing |
| `ad_copy` | AdCopySkill | Marketing |
| `content_repurpose` | ContentRepurposeSkill | Marketing |
| `content_calendar` | ContentCalendarSkill | Marketing |

### Agent personas

| Agent | Strengths | Model | Max tokens |
|-------|-----------|-------|-----------|
| **Engineer** | Code, architecture, GitHub | Sonnet | 8096 |
| **Writer** | Drafts, emails, long-form | Sonnet | 4096 |
| **Researcher** | Analysis, comparisons, TL;DR-first | Sonnet | 4096 |
| **Strategist** | Decisions, frameworks, go-to-market | Sonnet | 4096 |
| **Marketing** | Platform-native copy, AIDA/PAS/BAB, 3 variations | Sonnet | 6000 |
| **Default** | General assistant (Brain persona) | Sonnet | 2048 |

Agent selection is automatic — intent match takes priority, then keyword scan, then Default.

---

## Cost Tracking & Rate Limiting

Every LLM call is metered in real time. A runaway Slack loop cannot burn unbounded API credits.

### How it works

```
Incoming message
  │
  ▼ RateLimiter.check(session_id)        Redis INCR + TTL — no LLM cost
  │  → over limit: return ⏱️ reply immediately
  │
  ▼ [hooks, memory, intent, skill...]
  │
  ▼ CostTracker.check_budget(model)      Redis GET — < 1ms
  │  → over ceiling: return ⚠️ reply, no API call made
  │
  ▼ Anthropic API call
  │
  ▼ CostTracker.record(model, in, out)
       atomic Redis pipeline:
         INCRBYFLOAT brain:cost:daily:{date}:total
         INCRBY      brain:cost:daily:{date}:model:{model}:tokens_in/out
         EXPIRE      all keys 48hr (auto-cleanup, no cron needed)
       → checks thresholds → Slack alert (sync WebClient, safe in thread pool)
       → updates Prometheus gauges (brain_cost_usd_daily, brain_cost_ceiling_usd)
```

### Configuration (`.env`)

```bash
DAILY_COST_CEILING_USD=10.0          # hard ceiling; 0 = disabled
BUDGET_ALERT_THRESHOLDS=0.5,0.8,1.0  # Slack alerts at 50%, 80%, 100%
SLACK_ALERT_CHANNEL=brain-alerts
SONNET_DAILY_TOKEN_BUDGET=0          # 0 = no token limit
HAIKU_DAILY_TOKEN_BUDGET=0
RATE_LIMIT_PER_MINUTE=20
RATE_LIMIT_PER_HOUR=200
```

### Pricing

| Model | Input | Output |
|-------|-------|--------|
| `claude-sonnet-4-6` | $3.00 / 1M tokens | $15.00 / 1M tokens |
| `claude-haiku-4-5-20251001` | $0.25 / 1M tokens | $1.25 / 1M tokens |

Update `PRICING` in `app/brain/cost_tracker.py` when Anthropic changes rates.

### Behavior at limits

| Condition | Reply returned | API call made? |
|-----------|---------------|----------------|
| Session > `RATE_LIMIT_PER_MINUTE` | `⏱️ Too many requests — N in the last minute (limit: 20).` | No |
| Session > `RATE_LIMIT_PER_HOUR` | `⏱️ Too many requests — N in the last hour (limit: 200).` | No |
| Daily cost ≥ ceiling | `⚠️ Daily API budget reached. Back at midnight UTC.` | No |
| Model tokens ≥ budget | Same ⚠️ reply | No |

### Slack budget alerts

Sent to `SLACK_ALERT_CHANNEL`. Each threshold fires at most once per day.

```
💰 Brain API spend at 50% of daily ceiling
  Spent:     $5.0000 of $10.00
  Remaining: $5.0000
  Ceiling resets at midnight UTC. Check GET /api/v1/costs for breakdown.
```

At 100% the header escalates to `🚨 DAILY COST CEILING HIT — LLM calls are now BLOCKED`.

---

## Memory System

Three tiers work together automatically. No configuration is required.

| Tier | Storage | Contents | Lifetime |
|------|---------|----------|----------|
| **Hot** | Redis `brain:session:*` | Last 20 turns of the current session | 4 hours (TTL) |
| **Warm** | Postgres `session_summaries` | Haiku-generated summary, written every 10 turns | Permanent |
| **Cold** | Qdrant `brain_memories` | Semantic embeddings of high-signal turns (>200 chars) + rated ≥ 8 | Permanent |

On each request the dispatcher fetches all three tiers in parallel and prepends warm + cold context to the LLM prompt before calling the API.

---

## Task Queue — Celery + Flower

Three containers handle all background work:

| Container | What it does |
|-----------|-------------|
| `ai-celery-worker` | Executes tasks from the `evals` and `celery` queues (2 concurrent workers) |
| `ai-celery-beat` | Fires scheduled tasks based on the cron defined in `app/worker/celery_app.py` |
| `ai-flower` | Web UI for monitoring tasks in real time |

### Scheduled jobs

| Task | Schedule | What it does |
|------|----------|-------------|
| `run_weekly_agent_evals` | Sunday 09:00 UTC | Runs all agent eval test cases, posts Slack scorecard |
| `run_nightly_integration_evals` | 02:00 UTC daily | Read-only checks of Gmail, Calendar, GitHub, n8n, Home Assistant |

Both tasks retry up to 2 times on failure. The beat schedule file is persisted in the `celery-beat-data` Docker volume — missed runs are recovered after restarts.

### Celery broker layout

| Redis DB | Purpose |
|----------|---------|
| DB 0 | App hot memory (`brain:session:*`, `brain:cost:*`, `brain:rate:*`) |
| DB 1 | Celery broker (task queue) |
| DB 2 | Celery result backend (task results, 24hr TTL) |

### CLI commands

```bash
# Trigger a task right now (no need to wait for the schedule)
docker compose exec celery-worker \
  celery -A app.worker.celery_app call app.worker.tasks.run_weekly_agent_evals

docker compose exec celery-worker \
  celery -A app.worker.celery_app call app.worker.tasks.run_nightly_integration_evals

# Inspect running workers and what they're doing
docker compose exec celery-worker \
  celery -A app.worker.celery_app inspect active

# See all scheduled beat jobs and their next run time
docker compose exec celery-beat \
  celery -A app.worker.celery_app inspect scheduled

# Check queue depth
docker compose exec celery-worker \
  celery -A app.worker.celery_app inspect reserved
```

---

## Observability

Four telemetry points fire on every request: **request received → skill dispatched → LLM called → response delivered**. Three independent layers consume those events.

### Layer 1 — Structured logging (Loguru)

Two sinks start automatically:

| Sink | Format | Where |
|------|--------|-------|
| stdout | Colorized, human-readable | `docker compose logs brain` |
| File | Newline-delimited JSON, rotated midnight, 7-day retention, gzip | `LOG_DIR/brain.json` |

Every request emits four log lines:

```
REQUEST  | session=abc123 | src=slack | msg=What do I have tomorrow?...
SKILL    | calendar_read  | ctx=True | 12ms
LLM      | model=claude-sonnet-4-6 | in=1240 | out=312 | 1840ms
COST     | model=claude-sonnet-4-6 | call=$0.000018 | day=$0.0234
RESPONSE | session=abc123 | intent=calendar_read | agent=default | 1852ms
```

Set `LOG_LEVEL=DEBUG` to see the full hook chain, memory tier hits, and intent classifier output.

### Layer 2 — Real-time event stream (WebSocket)

```bash
# Watch live events with wscat
npm install -g wscat
wscat -c wss://your-domain.com/api/v1/observe/stream

# Python client
python3 -c "
import asyncio, websockets, json
async def watch():
    async with websockets.connect('ws://localhost:8000/api/v1/observe/stream') as ws:
        async for msg in ws:
            e = json.loads(msg)
            if e.get('event') != 'heartbeat':
                print(e)
asyncio.run(watch())
"
```

Events emitted per request:

```jsonc
{"event":"request_received","session_id":"abc","source":"slack","message_preview":"What do I..."}
{"event":"skill_dispatched","session_id":"abc","intent":"calendar_read","skill":"calendar_read","has_context":true,"latency_ms":12}
{"event":"llm_called","model":"claude-sonnet-4-6","agent":"default","input_tokens":1240,"output_tokens":312,"latency_ms":1840}
{"event":"response_delivered","session_id":"abc","intent":"calendar_read","agent":"default","latency_ms":1852,"reply_length":890,"success":true}
```

In-memory aggregate metrics:

```bash
curl https://your-domain.com/api/v1/observe/metrics
# → uptime, total requests, error rate, p50/p95/p99 latency, intent/agent/model breakdowns, token totals

curl https://your-domain.com/api/v1/observe/events?limit=20
# → last N raw events
```

### Layer 3 — Prometheus + Grafana metrics

See the [Dashboards](#dashboards) section for how to navigate the Grafana UI.

**Custom brain metrics** (exposed at `/metrics`, scraped every 15s):

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `brain_requests_total` | Counter | `intent`, `agent`, `success` | Total chat requests processed |
| `brain_response_latency_seconds` | Histogram | `intent`, `agent` | End-to-end response latency |
| `brain_llm_tokens_total` | Counter | `model`, `direction` | LLM tokens consumed |
| `brain_llm_latency_seconds` | Histogram | `model` | LLM API round-trip time |
| `brain_skill_duration_seconds` | Histogram | `skill` | Skill execution duration |
| `brain_cost_usd_daily` | Gauge | — | Total LLM cost today (USD) |
| `brain_cost_ceiling_usd` | Gauge | — | Configured daily ceiling (USD) |
| `brain_budget_exceeded_total` | Counter | — | Calls blocked by budget ceiling |
| `brain_rate_limited_total` | Counter | `window` | Requests blocked by rate limiter |

**Infrastructure metrics** (from exporters):

| Source | Key metrics |
|--------|-------------|
| `redis-exporter` | `redis_memory_used_bytes`, `redis_connected_clients`, `redis_keyspace_hits_total` |
| `celery-exporter` | `celery_tasks_total{state}`, `celery_queue_length`, `celery_workers_online` |

### Layer 4 — Sentry error tracking

```bash
SENTRY_DSN=https://your-key@sentry.io/your-project-id
```

Sentry captures skill failures, exhausted LLM retries, and any unhandled exception. `WebSocketDisconnect` and `CancelledError` are filtered out as noise. Errors are tagged with `intent`, `agent`, and `session_id`.

Get a free DSN at [sentry.io](https://sentry.io) — the free tier covers well over a personal brain's error volume.

---

## Eval System

Automated quality checks run on two schedules via Celery Beat.

### Schedules

| Job | When | What happens |
|-----|------|-------------|
| Agent quality evals | Sunday 09:00 UTC | 3 test cases × 6 agents, Haiku as judge, Slack scorecard posted |
| Integration reliability | 02:00 UTC nightly | Read-only API checks of Gmail, Calendar, GitHub, n8n, Home Assistant |

### Agents evaluated

Engineer · Writer · Researcher · Strategist · Marketing · Default

### Slack scorecard format

```
🧠 Weekly Brain Eval Report — 2026-03-01

✅  *Engineer    *  8.4/10  (+0.2 vs last week)  [3/3 passed]
✅  *Marketing   *  7.9/10  (baseline)            [3/3 passed]
✅  *Researcher  *  8.1/10  (+0.5 vs last week)   [3/3 passed]
✅  *Strategist  *  7.6/10  (-0.1 vs last week)   [3/3 passed]
✅  *Writer      *  8.0/10  (+0.3 vs last week)   [3/3 passed]

*Integration uptime (7d rolling):*
  Gmail: ✅ 100%  ·  Calendar: ✅ 100%  ·  GitHub: ✅ 98.6%  ·  n8n: ✅ 100%  ·  HA: ⚠️ 85.7%
```

### Run evals manually

```bash
# All agent evals
python3 evals/run_evals.py

# One agent
python3 evals/run_evals.py --agent engineer

# Single test case
python3 evals/run_evals.py --agent writer --test test_02_rewrite_for_clarity

# Nightly integration checks
python3 evals/run_evals.py --nightly

# Post results to Slack immediately
python3 evals/run_evals.py --slack
python3 evals/run_evals.py --nightly --slack
```

### Adding a test case

Create `evals/agents/<agent>/test_XX_name.json`:

```json
{
  "input": "The prompt sent to the agent",
  "criteria": [
    "what the response must include",
    "another measurable criterion"
  ],
  "judge_prompt": "Context telling the Haiku judge what a good answer looks like.",
  "threshold": 7
}
```

`threshold` is the minimum score (0–10) to pass. Default is 7.

### Postgres tables

```sql
-- Per-test results with run_id grouping for trend tracking
SELECT agent_name, AVG(score), COUNT(*) FILTER (WHERE passed) AS passed
FROM eval_results
WHERE created_at > NOW() - INTERVAL '30 days'
GROUP BY agent_name;

-- 7-day integration uptime
SELECT integration,
       ROUND(COUNT(*) FILTER (WHERE passed)::numeric / COUNT(*) * 100, 1) AS uptime_pct
FROM integration_eval_results
WHERE checked_at > NOW() - INTERVAL '7 days'
GROUP BY integration;
```

---

## Security

The `SecurityHook` runs before every message and blocks common prompt injection patterns:

- `[INST]` / `<<SYS>>` / `<|im_start|>` injection tokens
- "ignore previous instructions" and similar phrases
- DAN / jailbreak phrases
- System prompt exfiltration attempts ("show me your system prompt")
- Identity confusion ("you are now", "pretend to be")

Blocked messages return a safe reply and are logged with the matched pattern. The pattern list lives in `app/security/patterns.py`.

---

## Google OAuth Setup

Run once on the server to generate a refresh token:

```bash
python3 scripts/google_auth.py
```

Follow the browser prompt. Paste the resulting refresh token into `.env` as `GOOGLE_REFRESH_TOKEN`.

---

## Updating

```bash
# Pull latest code
git pull

# Rebuild everything that changed
docker compose build --no-cache brain celery-worker celery-beat flower
docker compose up -d

# Or rebuild just the brain
docker compose build --no-cache brain && docker compose up -d brain
```

Database schema changes (`app/db/schema.sql`) run automatically on startup — all `CREATE TABLE` statements use `IF NOT EXISTS` so no data is ever lost.

---

## Troubleshooting

**Brain container exits on startup**
```bash
docker compose logs brain
```
Usually a missing `.env` value or Postgres not yet healthy. Confirm `ANTHROPIC_API_KEY` is set and non-empty.

**Slack bot not responding**
- Verify `SLACK_APP_TOKEN` starts with `xapp-` (Socket Mode token, not a webhook URL)
- Check `docker compose logs brain | grep -i slack`

**Gmail / Calendar returning "not configured"**
- Confirm `GOOGLE_REFRESH_TOKEN` is in `.env`
- Re-run `python3 scripts/google_auth.py` if the token has expired

**Qdrant embeddings are zero vectors (semantic search not working)**
- Add `OPENAI_API_KEY` to `.env` — the system falls back to zero vectors when it's missing, which makes semantic search return random results

**Grafana shows "No data" on all panels**
- Check Prometheus targets: open `http://localhost:9090/targets` via SSH tunnel — all four targets should be `UP`
- If `brain` target is `DOWN`: `docker compose logs brain | grep metrics`
- If `redis` target is `DOWN`: `docker compose logs redis-exporter`

**Grafana login not working**
- Confirm `GRAFANA_USER` and `GRAFANA_PASSWORD` are set in `.env`
- Restart Grafana: `docker compose restart grafana`

**Flower shows no workers**
- Workers need 10–15s to connect after startup
- Check: `docker compose logs celery-worker | grep -i "ready"`
- Verify broker connection: `docker compose logs celery-worker | grep -i "redis"`

**Budget ceiling hit — brain is blocked**
- Check current spend: `curl https://your-domain.com/api/v1/costs`
- To raise the ceiling immediately (no restart needed):
  - Edit `.env`: increase `DAILY_COST_CEILING_USD`
  - The ceiling is read on every call — takes effect instantly
- Or manually clear the daily counter in Redis:
  ```bash
  docker compose exec redis redis-cli -a "$REDIS_PASSWORD" \
    DEL "brain:cost:daily:$(date +%Y-%m-%d):total"
  ```

**Redis connection refused**
- Check `REDIS_PASSWORD` matches between `.env` and the Redis container healthcheck
- `docker compose ps` — confirm `ai-redis` shows `healthy`

**Reset a specific session**
```bash
curl -X DELETE https://your-domain.com/api/v1/chat/your-session-id
```

**Celery tasks not running on schedule**
- Verify `ai-celery-beat` is running: `docker compose ps celery-beat`
- Check beat logs: `docker compose logs celery-beat`
- Verify the schedule file exists: `docker compose exec celery-beat ls /app/celerybeat-data/`
