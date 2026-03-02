# AI Brain — CSuite Code

Personalized AI assistant for Anthony. Runs on an Ubuntu server as a FastAPI/Python application. Slack is the primary interface; a REST API is also available for direct access and automation.

---

## Architecture

```
Slack / REST API
       │
       ▼
  SecurityHook  ──── blocks injection attempts
       │
  Dispatcher
  ├── MemoryManager
  │    ├── Redis      (hot — current session, 4hr TTL)
  │    ├── Postgres   (warm — session summaries, flushed every 10 turns)
  │    └── Qdrant     (cold — semantic embeddings, high-signal turns)
  ├── IntentClassifier  (Haiku — classifies message → intent + params)
  ├── AgentRegistry     (selects persona: Engineer / Writer / Researcher / Strategist / Default)
  ├── SkillRegistry     (routes to Gmail / Calendar / GitHub / Home Assistant / n8n / Chat)
  └── LLMRouter         (Claude Sonnet — Agent.prompt + TELOS personal context)
       │
  LoggingHook  ──── logs latency, intent, agent
       │
  DispatchResult (reply, intent, agent, session_id)
```

**Services (Docker Compose):**

| Container | Role | Port (local) |
|-----------|------|-------------|
| `ai-brain` | FastAPI app | 8000 |
| `ai-postgres` | Warm memory + ratings | 5432 |
| `ai-redis` | Hot session memory | 6379 |
| `ai-qdrant` | Vector / cold memory | 6333 |
| `ai-n8n` | Workflow automation | 5678 |
| `ai-nginx` | Reverse proxy + SSL | 80, 443 |

---

## Prerequisites

- Ubuntu 22.04 server (1 vCPU / 2 GB RAM minimum; 2 vCPU / 4 GB recommended)
- A domain name pointing to the server's IP
- Anthropic API key (required)
- Optional: OpenAI API key (for semantic embeddings; falls back to mock vectors without it), Google OAuth credentials, GitHub token, Slack app credentials

---

## Deployment

### 1. Bootstrap the server (run once)

```bash
# On the server as a sudo user (not root)
chmod +x scripts/server_setup.sh
./scripts/server_setup.sh
```

This installs Docker, Docker Compose, configures UFW (ports 22/80/443), and enables Fail2ban. Log out and back in after it completes so Docker group membership takes effect.

### 2. Copy project files to the server

```bash
# From your local machine
scp -r . user@your-server:~/ai-brain/
```

### 3. Configure environment

```bash
cd ~/ai-brain
cp .env.example .env
nano .env
```

Fill in every value:

```bash
# ── Core ──────────────────────────────────────────────────────
SECRET_KEY=<generate: openssl rand -hex 32>
ENVIRONMENT=production

# ── PostgreSQL ─────────────────────────────────────────────────
POSTGRES_USER=brain
POSTGRES_PASSWORD=<strong password>
POSTGRES_DB=aibrain

# ── Redis ──────────────────────────────────────────────────────
REDIS_PASSWORD=<strong password>

# ── LLM ────────────────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...          # optional — enables real embeddings in Qdrant

# ── Slack ──────────────────────────────────────────────────────
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_APP_TOKEN=xapp-...       # Socket Mode token

# ── n8n ────────────────────────────────────────────────────────
N8N_HOST=your-domain.com
N8N_USER=admin
N8N_PASSWORD=<strong password>
N8N_WEBHOOK_URL=http://n8n:5678

# ── Domain ─────────────────────────────────────────────────────
DOMAIN=your-domain.com

# ── Google (Gmail + Calendar) ──────────────────────────────────
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=...       # run: python3 scripts/google_auth.py

# ── GitHub ─────────────────────────────────────────────────────
GITHUB_TOKEN=ghp_...
GITHUB_USERNAME=your-username
GITHUB_DEFAULT_REPO=your-username/your-repo

# ── Home Assistant ─────────────────────────────────────────────
HOME_ASSISTANT_URL=http://192.168.1.100:8123
HOME_ASSISTANT_TOKEN=...

# ── TELOS ──────────────────────────────────────────────────────
TELOS_DIR=/home/ubuntu/ai-brain/telos

# ── Observability ───────────────────────────────────────────────
SENTRY_DSN=https://your-key@sentry.io/your-project  # leave blank to disable
LOG_LEVEL=INFO                                        # DEBUG for verbose output
LOG_DIR=/var/log/aibrain
```

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

This builds the brain container and starts all services. Verify:

```bash
curl https://your-domain.com/
# → {"status":"Brain is alive","version":"2.0.0"}
```

---

## Managing the deployment

```bash
# Start / rebuild
./scripts/deploy.sh

# Restart without rebuild
./scripts/deploy.sh restart

# Tail logs
./scripts/deploy.sh logs

# Show container status
./scripts/deploy.sh status

# Stop everything
./scripts/deploy.sh stop

# Rebuild only the brain (after code changes)
docker compose build --no-cache brain && docker compose up -d brain
```

---

## TELOS — Personal Context

The `telos/` directory holds Markdown files that are injected into every LLM system prompt. Edit them to keep the brain's context current:

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

After editing any file, reload without restarting:

```bash
curl -X POST https://your-domain.com/api/v1/telos/reload
```

Or just wait — the cache auto-refreshes every 5 minutes.

---

## Slack Interface

The brain listens via Socket Mode — no public webhook required.

**Send a message:**
- DM the bot directly, or
- `@Brain` mention it in any channel

**Examples:**

```
# General question
What should I focus on this week?

# Code help
Debug this Python function: [paste code]

# Email
Draft an email to sarah@company.com about the Q1 proposal

# Calendar
What do I have tomorrow?

# GitHub
What are the open issues in my-repo?

# Smart home
Turn off the living room lights

# Confirm a write action
confirm
```

Write actions (email send, calendar create) require a `confirm` reply before executing. Say `cancel` to abort.

---

## REST API

Base URL: `https://your-domain.com`

Interactive docs (non-production): `https://your-domain.com/docs`

### Chat

```bash
# Send a message
curl -X POST https://your-domain.com/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What is the current status of my GitHub issues?",
    "session_id": "my-session"
  }'

# Response
{
  "reply": "...",
  "session_id": "my-session",
  "intent": "github_read",
  "agent": "engineer"
}

# Clear a session
curl -X DELETE https://your-domain.com/api/v1/chat/my-session
```

### Agents

```bash
# List all registered agent personalities
curl https://your-domain.com/api/v1/agents

# Response
{
  "agents": [
    {"name": "engineer",   "preferred_model": "claude-sonnet-4-6", "max_tokens": 8096, ...},
    {"name": "writer",     "preferred_model": "claude-sonnet-4-6", "max_tokens": 4096, ...},
    {"name": "researcher", "preferred_model": "claude-sonnet-4-6", "max_tokens": 4096, ...},
    {"name": "strategist", "preferred_model": "claude-sonnet-4-6", "max_tokens": 4096, ...},
    {"name": "default",    "preferred_model": "claude-sonnet-4-6", "max_tokens": 2048, ...}
  ]
}
```

Agent selection is automatic based on intent and keywords. No parameter needed.

### TELOS

```bash
# Force reload personal context files from disk
curl -X POST https://your-domain.com/api/v1/telos/reload

# Response
{"reloaded": ["beliefs.md", "context.md", "goals.md", ...]}
```

### Integrations

```bash
# Check which integrations are configured
curl https://your-domain.com/api/v1/integrations/status

# Gmail — list unread emails
curl "https://your-domain.com/api/v1/integrations/gmail?query=is:unread&max_results=5"

# Calendar — upcoming events
curl "https://your-domain.com/api/v1/integrations/calendar?period=this+week"

# GitHub — open issues
curl "https://your-domain.com/api/v1/integrations/github/issues?repo=owner/repo"

# GitHub — notifications
curl https://your-domain.com/api/v1/integrations/github/notifications

# GitHub — open PRs
curl "https://your-domain.com/api/v1/integrations/github/prs?repo=owner/repo"

# Home Assistant — all entity states
curl https://your-domain.com/api/v1/integrations/home-assistant/states

# Home Assistant — specific entity
curl https://your-domain.com/api/v1/integrations/home-assistant/entity/light.living_room

# Home Assistant — call a service
curl -X POST https://your-domain.com/api/v1/integrations/home-assistant/service \
  -H "Content-Type: application/json" \
  -d '{"domain": "light", "service": "turn_off", "data": {"entity_id": "light.living_room"}}'

# n8n — trigger a workflow
curl -X POST https://your-domain.com/api/v1/integrations/n8n/trigger \
  -H "Content-Type: application/json" \
  -d '{"workflow": "daily_brief", "payload": {}}'
```

### Feedback / Ratings

Rate individual responses to improve quality over time. Ratings ≥ 8 are automatically stored in Qdrant and surfaced in future relevant conversations.

```bash
# Rate a specific message (1-10)
curl -X POST https://your-domain.com/api/v1/feedback/rate \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "my-session",
    "message_index": 0,
    "rating": 9,
    "comment": "Perfect response format",
    "intent": "code"
  }'

# Thumbs up / down
curl -X POST https://your-domain.com/api/v1/feedback/thumbs \
  -H "Content-Type: application/json" \
  -d '{"session_id": "my-session", "message_index": 0, "positive": true}'

# View aggregate stats
curl https://your-domain.com/api/v1/feedback/summary

# Response
{
  "total_ratings": 47,
  "avg_rating": 8.3,
  "unique_sessions": 12,
  "top_intent": "code"
}
```

### Health

```bash
curl https://your-domain.com/api/v1/health
# → {"status": "ok", "redis": true, "postgres": true}
```

---

## Intent Routing

The brain uses Claude Haiku to classify every message into an intent. The intent determines which skill runs and which agent persona responds.

| Intent | Skill | Agent |
|--------|-------|-------|
| `chat` | ChatSkill (no external call) | Default or keyword-matched |
| `code` | CodeSkill (routing hint) | Engineer |
| `research` | ResearchSkill (Qdrant search) | Researcher |
| `gmail_read` | GmailReadSkill | Default |
| `gmail_send` | GmailSendSkill (confirmation required) | Writer |
| `calendar_read` | CalendarReadSkill | Default |
| `calendar_write` | CalendarWriteSkill | Default |
| `github_read` | GitHubReadSkill | Engineer |
| `github_write` | GitHubWriteSkill | Engineer |
| `smart_home` | SmartHomeSkill | Default |
| `n8n_execute` | N8nSkill | Default |

---

## Security

The `SecurityHook` runs before every message and blocks common prompt injection patterns:

- `[INST]` / `<<SYS>>` / `<|im_start|>` injection tokens
- "ignore previous instructions" and variants
- DAN / jailbreak phrases
- System prompt exfiltration attempts ("show me your system prompt")
- Identity confusion ("you are now", "pretend to be")

Blocked messages return a safe error reply and are logged with the matched pattern.

---

## Memory System

Three tiers work together automatically:

| Tier | Storage | Scope | TTL |
|------|---------|-------|-----|
| Hot | Redis | Current session history (last 20 turns) | 4 hours |
| Warm | Postgres `session_summaries` | Haiku-generated summary of past sessions | Permanent |
| Cold | Qdrant `brain_memories` | Semantic embeddings of high-signal turns | Permanent |

Postgres is flushed every 10 turns per session and on session end. Qdrant stores any exchange longer than 200 characters plus all interactions rated ≥ 8.

---

## Observability

Three-layer approach. All three work independently — any layer can be skipped.

### Layer 1 — Structured logging (Loguru)

Two sinks, configured automatically on startup:

| Sink | Format | Location |
|------|--------|----------|
| stdout | Human-readable, colorized | Docker logs (`docker compose logs brain`) |
| File | Newline-delimited JSON, rotated midnight, 7-day retention | `LOG_DIR` (default `/var/log/aibrain/brain.json`) |

Every request cycle emits structured logs at 4 points:

```
REQUEST   | session=abc123 | src=slack | msg=What do I have tomorrow?...
SKILL     | calendar_read  | intent=calendar_read | ctx=True | 12ms
LLM       | model=claude-sonnet-4-6 | in=1240 | out=312 | 1840ms
RESPONSE  | session=abc123 | intent=calendar_read | agent=default | 1852ms | 890c
```

Set `LOG_LEVEL=DEBUG` in `.env` to see all internal events (intent classifier, memory tier hits, hook chain).

### Layer 2 — Real-time observability stream (WebSocket)

Connect to the event stream and see every Brain lifecycle event as it happens:

```bash
# Quick test with wscat
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

**Event types emitted (one request → 4 events):**

```jsonc
// 1. Received
{"event":"request_received","session_id":"abc","source":"slack","message_preview":"What do I..."}

// 2. LLM called (after skill, before response)
{"event":"llm_called","model":"claude-sonnet-4-6","agent":"default","input_tokens":1240,"output_tokens":312,"latency_ms":1840}

// 3. Skill ran
{"event":"skill_dispatched","session_id":"abc","intent":"calendar_read","skill":"calendar_read","has_context":true,"latency_ms":12}

// 4. Done
{"event":"response_delivered","session_id":"abc","intent":"calendar_read","agent":"default","latency_ms":1852,"reply_length":890,"success":true}
```

**Aggregate metrics:**

```bash
curl https://your-domain.com/api/v1/observe/metrics
```
```json
{
  "uptime_seconds": 3600,
  "total_requests": 84,
  "total_errors": 2,
  "error_rate": 0.0238,
  "latency_ms": {"avg": 1920.3, "p50": 1740.1, "p95": 3200.0, "p99": 4100.0},
  "intents":  {"chat": 40, "calendar_read": 18, "code": 15, "gmail_read": 11},
  "agents":   {"default": 45, "engineer": 15, "marketing": 12, "researcher": 12},
  "models":   {"claude-sonnet-4-6": 82, "claude-haiku-4-5-20251001": 84},
  "tokens":   {"total_input": 104200, "total_output": 26100},
  "recent_errors": []
}
```

```bash
# Recent raw events (last 50)
curl https://your-domain.com/api/v1/observe/events?limit=20
```

### Layer 3 — Sentry error tracking

Add your DSN to `.env`:
```bash
SENTRY_DSN=https://your-key@sentry.io/your-project-id
```

Sentry captures:
- **Skill failures** — integration returned an error or timed out
- **LLM failures** — all 3 Tenacity retries exhausted
- **Unhandled exceptions** — anything that bubbles up to FastAPI
- **Slow requests** — via performance tracing (20% sample rate by default)

Errors are grouped by type, tagged with `intent`, `agent`, and `session_id` as extra context. Stack traces link to the exact line.

Get a free DSN at [sentry.io](https://sentry.io) — the free tier (5k errors/month) is more than enough for a personal brain.

---

## Task Queue — Celery + Flower

Celery runs all background work. Three dedicated containers handle it:

| Container | Role |
|-----------|------|
| `ai-celery-worker` | Executes tasks (2 concurrent workers, `evals` + `celery` queues) |
| `ai-celery-beat` | Cron scheduler — fires tasks on schedule (replaces APScheduler) |
| `ai-flower` | Real-time task monitoring UI |

### Flower UI

Access the Flower dashboard at `https://your-domain.com/flower/`

Login with the credentials from `.env`:
```bash
FLOWER_USER=admin
FLOWER_PASSWORD=<strong password>
```

Flower shows:
- Active workers and their concurrency
- Task history (succeeded / failed / retried)
- Queue depths per queue
- Task runtime and argument details

### Running tasks manually

```bash
# Trigger the weekly eval run right now (doesn't wait for Sunday)
docker compose exec celery-worker celery -A app.worker.celery_app call \
  app.worker.tasks.run_weekly_agent_evals

# Same for nightly integration checks
docker compose exec celery-worker celery -A app.worker.celery_app call \
  app.worker.tasks.run_nightly_integration_evals

# Inspect active workers
docker compose exec celery-worker celery -A app.worker.celery_app inspect active

# See scheduled Beat jobs
docker compose exec celery-beat celery -A app.worker.celery_app inspect scheduled
```

### Celery config notes

- **Broker**: Redis DB 1 (app hot memory uses DB 0 — no collision)
- **Backend**: Redis DB 2 (task results stored 24 hr)
- **Beat schedule file**: persisted in the `celery-beat-data` Docker volume so missed runs are tracked across restarts

---

## Prometheus + Grafana

Infrastructure metrics are scraped by Prometheus and visualised in Grafana.

### Access

| UI | URL | Auth |
|----|-----|------|
| Grafana dashboards | `https://your-domain.com/grafana/` | `GRAFANA_USER` / `GRAFANA_PASSWORD` |
| Prometheus (internal) | `http://localhost:9090` (bind to 127.0.0.1) | none |

Add to `.env`:
```bash
GRAFANA_USER=admin
GRAFANA_PASSWORD=<strong password>
FLOWER_USER=admin
FLOWER_PASSWORD=<strong password>
```

### What's scraped

| Exporter | Metrics |
|----------|---------|
| `brain:8000/metrics` | HTTP request rate, latency, status codes (auto) + brain-specific counters |
| `redis-exporter:9121` | Redis memory, connected clients, hit/miss rate, commands/s |
| `celery-exporter:9808` | Task queue depth, success/failure rate, worker count, task duration |

### Custom brain metrics (from `app/observability/prometheus_metrics.py`)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `brain_requests_total` | Counter | `intent`, `agent`, `success` | Total chat requests |
| `brain_response_latency_seconds` | Histogram | `intent`, `agent` | End-to-end latency |
| `brain_llm_tokens_total` | Counter | `model`, `direction` | Input and output tokens |
| `brain_llm_latency_seconds` | Histogram | `model` | LLM API round-trip time |
| `brain_skill_duration_seconds` | Histogram | `skill` | Skill execution time |

These are emitted automatically at the four existing telemetry points (`request_received`, `llm_called`, `skill_dispatched`, `response_delivered`) with no code changes needed in the request path.

### Brain Overview dashboard

The pre-built `brain_overview.json` dashboard loads automatically in Grafana and shows:

- **Request rate** and **error rate** (stat + time series)
- **P95 / P50 / P99 response latency** over time
- **LLM tokens/minute** by model and direction (input vs output)
- **LLM latency** by model (p50 and p95)
- **Requests by intent** (last 1h bar chart)
- **Celery tasks by state** over time (success / failure / retry)
- **Celery queue depth** (current)
- **Redis memory** usage
- **Redis hit rate** and **connected clients**

Dashboard auto-refreshes every 30 seconds. Default time window is the last 6 hours.

### Useful PromQL queries

```promql
# Average response latency (last 5 min)
histogram_quantile(0.95, sum by(le) (rate(brain_response_latency_seconds_bucket[5m]))) * 1000

# Token spend rate per model
rate(brain_llm_tokens_total[5m]) * 60

# Celery task failure rate
rate(celery_tasks_total{state="FAILURE"}[5m]) / rate(celery_tasks_total[5m])

# Redis memory used
redis_memory_used_bytes

# Intent breakdown (last hour)
sum by(intent) (increase(brain_requests_total[1h]))
```

### Retention

Prometheus stores 30 days of metrics in the `prometheus-data` Docker volume. Grafana layout and any custom dashboards you create persist in `grafana-data`.

---

## Eval System

The brain runs automated quality checks on two schedules. Results are stored in Postgres and posted to Slack.

### Schedules

| Job | Schedule | What it does |
|-----|----------|-------------|
| Agent quality evals | Sunday 09:00 UTC | 3 test cases per agent, Haiku judge, posts Slack scorecard |
| Integration reliability | 02:00 UTC nightly | Read-only checks of Gmail, Calendar, GitHub, n8n, Home Assistant |

Both jobs start automatically with the brain server. Missed runs are retried for up to 1 hour.

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

Configure the channel in `.env`:

```bash
SLACK_EVAL_CHANNEL=brain-evals   # defaults to "brain-evals" if unset
```

### Run evals manually

```bash
# Run from the project root

# All agent evals
python3 evals/run_evals.py

# One agent only
python3 evals/run_evals.py --agent engineer

# Single test case
python3 evals/run_evals.py --agent writer --test test_02_rewrite_for_clarity

# Nightly integration checks
python3 evals/run_evals.py --nightly

# Any of the above + post to Slack immediately
python3 evals/run_evals.py --slack
python3 evals/run_evals.py --nightly --slack
```

### Test case structure

Test cases live in `evals/agents/<agent>/test_*.json`. Add new ones by creating additional JSON files:

```json
{
  "input": "The prompt sent to the agent",
  "criteria": [
    "what the response must include or demonstrate",
    "another measurable criterion"
  ],
  "judge_prompt": "Context for the Haiku judge explaining what a good answer looks like.",
  "threshold": 7
}
```

`threshold` is the minimum score (0-10) for the test to pass. Default is 7.

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

## Google OAuth Setup

Run once to generate a refresh token:

```bash
python3 scripts/google_auth.py
```

Follow the browser prompt. Paste the resulting refresh token into `.env` as `GOOGLE_REFRESH_TOKEN`.

---

## Updating

```bash
# Pull latest code to the server
git pull

# Rebuild brain container only
docker compose build --no-cache brain
docker compose up -d brain

# Verify
curl https://your-domain.com/
```

Database schema changes run automatically on startup (`IF NOT EXISTS` guards prevent data loss on re-runs).

---

## Troubleshooting

**Brain container exits on startup**
```bash
docker compose logs brain
```
Usually a missing `.env` value or Postgres not yet healthy. Check `ANTHROPIC_API_KEY` is set.

**Slack bot not responding**
- Verify `SLACK_APP_TOKEN` starts with `xapp-` (Socket Mode token, not a webhook)
- Check `docker compose logs brain | grep -i slack`

**Gmail / Calendar returning "not configured"**
- Confirm `GOOGLE_REFRESH_TOKEN` is in `.env`
- Re-run `python3 scripts/google_auth.py` if the token has expired

**Qdrant embeddings are mock zeros**
- Add `OPENAI_API_KEY` to `.env` — the system falls back to zero vectors when it's missing, which means semantic search won't return meaningful results

**Redis connection refused**
- Check `REDIS_PASSWORD` matches between `.env` and the Redis healthcheck
- `docker compose ps` to confirm `ai-redis` is healthy

**Reset a session**
```bash
curl -X DELETE https://your-domain.com/api/v1/chat/your-session-id
```
