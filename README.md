# Sentinel AI Brain

Personalized AI assistant and autonomous operations platform. Runs on an Ubuntu server as a FastAPI/Python application with Celery workers, a distributed mesh agent network, and a full observability stack. Slack is the primary interface; a REST API and CLI are also available.

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
9. [CLI — brain.py](#cli--brainpy)
10. [REST API](#rest-api)
11. [Intent Routing & Agent Personas](#intent-routing--agent-personas)
12. [Skills Reference](#skills-reference)
13. [Task Board](#task-board)
14. [Sentinel Mesh Agent System](#sentinel-mesh-agent-system)
15. [RMM — MeshCentral Integration](#rmm--meshcentral-integration)
16. [Reddit News-Feed](#reddit-news-feed)
17. [Sentry Auto-Triage](#sentry-auto-triage)
18. [Cost Tracking & Rate Limiting](#cost-tracking--rate-limiting)
19. [Memory System](#memory-system)
20. [Cross-Interface Memory](#cross-interface-memory)
21. [Autonomy Mode](#autonomy-mode)
22. [Task Queue — Celery + Flower](#task-queue--celery--flower)
23. [Observability](#observability)
24. [Eval System](#eval-system)
25. [Security](#security)
26. [Google OAuth Setup](#google-oauth-setup)
27. [Updating](#updating)
28. [CI/CD — GitOps Loop](#cicd--gitops-loop)
29. [Troubleshooting](#troubleshooting)

---

## Architecture

```
Slack / CLI / REST API
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
  │    ├── Qdrant     (cold — semantic embeddings of high-signal turns)
  │    └── Neo4j      (graph — entity relationships, knowledge links)
  ├── IntentClassifier  (Haiku — classifies message → intent + params)
  ├── AgentRegistry     (selects persona based on intent + keywords)
  ├── SkillRegistry     (executes the matching skill, returns context data)
  └── LLMRouter
       ├── CostTracker.check_budget() ← blocks if daily ceiling hit
       ├── client.messages.create()   ← Anthropic API call
       └── CostTracker.record()       ← atomic Redis counters + Slack alert
       │
  LoggingHook  ──── Loguru structured log + WebSocket event bus emit
  MilestoneHook ─── logs every confirmed write action to DB + Slack
       │
  DispatchResult (reply, intent, agent, session_id)
```

**All containers (Docker Compose):**

| Container | Role | Internal port |
|-----------|------|---------------|
| `brain` | FastAPI application | 8000 |
| `postgres` | Warm memory, tasks, eval results, audit log | 5432 |
| `redis` | Hot session memory, Celery broker + backend, agent streams | 6379 |
| `qdrant` | Vector / cold memory | 6333 |
| `neo4j` | Knowledge graph (entity relationships) | 7474 / 7687 |
| `n8n` | Workflow automation | 5678 |
| `home-assistant` | Smart home integration | 8123 |
| `meshcentral` | RMM console (MeshCentral) | 4430 |
| `nginx` | Reverse proxy + SSL termination | 80, 443 |
| `certbot` | Let's Encrypt auto-renewal | — |
| `deploy-agent` | Webhook receiver for CI/CD | 9000 |
| `celery-worker` | Background task executor (evals, celery, tasks_general queues) | — |
| `celery-worker-workspace` | Serialized git/workspace operations (tasks_workspace queue) | — |
| `celery-beat` | Cron scheduler (15 scheduled jobs) | — |
| `flower` | Celery task monitoring UI | 5555 |
| `prometheus` | Metrics scraper and time-series storage | 9090 |
| `grafana` | Metrics dashboards | 3000 |
| `loki` | Log aggregation | 3100 |
| `promtail` | Log shipper (Docker → Loki) | — |
| `redis-exporter` | Exports Redis metrics to Prometheus | 9121 |
| `celery-exporter` | Exports Celery metrics to Prometheus | 9808 |
| `postgres-exporter` | Exports Postgres metrics to Prometheus | — |
| `node-exporter` | Host CPU/memory/disk metrics | — |
| `cadvisor` | Container resource metrics | — |

---

## Prerequisites

- Ubuntu 22.04 server (4 vCPU / 8 GB RAM recommended — full stack with Neo4j, Loki, and MeshCentral)
- A domain name pointing to the server's IP
- Anthropic API key (required)
- Optional: OpenAI API key (semantic embeddings — falls back to zero vectors without it), Google OAuth credentials, GitHub token, Slack app credentials, Sentry DSN, IONOS API token, Twilio credentials

---

## Environment Variables

Copy the example and fill in your values:

```bash
cp .env.example .env
nano .env
```

Full reference:

```bash
# ── Core ──────────────────────────────────────────────────────────────────────
SECRET_KEY=<generate: openssl rand -hex 32>
ENVIRONMENT=production           # development | production

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
POSTGRES_USER=brain
POSTGRES_PASSWORD=<strong password>
POSTGRES_DB=aibrain

# ── Redis ──────────────────────────────────────────────────────────────────────
REDIS_PASSWORD=<strong password>

# ── Neo4j (knowledge graph) ────────────────────────────────────────────────────
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<strong password>

# ── LLM APIs ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...     # required
OPENAI_API_KEY=sk-...            # optional — enables real Qdrant embeddings
GEMINI_API_KEY=...               # optional

# ── Slack ──────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN=xoxb-...         # Bot token for posting messages
SLACK_SIGNING_SECRET=...         # For request verification
SLACK_APP_TOKEN=xapp-...         # Socket Mode token (starts with xapp-)
SLACK_OWNER_USER_ID=U0XXXXXXXX   # Owner Slack user ID (for DM approvals)

# Slack channel IDs or names
SLACK_EVAL_CHANNEL=sentinel-evals
SLACK_ALERT_CHANNEL=sentinel-alerts
SLACK_MILESTONE_CHANNEL=sentinel-milestones
SLACK_TASKS_CHANNEL=sentinel-tasks
SLACK_RESEARCH_CHANNEL=sentinel-research
SLACK_RMM_PROD_CHANNEL=rmm-production
SLACK_RMM_DEV_CHANNEL=rmm-dev-staging
SLACK_AGENTS_CHANNEL=sentinel-agents
SLACK_REDDIT_CHANNEL=sentinel-reddit

# ── n8n ────────────────────────────────────────────────────────────────────────
N8N_HOST=your-n8n-host.com
N8N_USER=<n8n username>
N8N_PASSWORD=<n8n password>
N8N_WEBHOOK_URL=http://n8n:5678
N8N_API_KEY=...

# ── Domain ─────────────────────────────────────────────────────────────────────
DOMAIN=your-domain.com

# ── Google (primary account) ───────────────────────────────────────────────────
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=...         # run: python3 scripts/google_auth.py
GOOGLE_CALENDAR_ID=primary

# Optional: additional Google accounts (up to 4 extra)
GOOGLE_ACCOUNT_2_NAME=work
GOOGLE_ACCOUNT_2_CLIENT_ID=...
GOOGLE_ACCOUNT_2_CLIENT_SECRET=...
GOOGLE_ACCOUNT_2_REFRESH_TOKEN=...
GOOGLE_ACCOUNT_2_CALENDAR_ID=...

# ── GitHub ─────────────────────────────────────────────────────────────────────
GITHUB_TOKEN=ghp_...
GITHUB_USERNAME=your-username
GITHUB_DEFAULT_REPO=your-username/your-repo
GITHUB_WEBHOOK_SECRET=...
GITHUB_BRAIN_REPO_URL=git@github.com:your-username/your-repo.git  # self-modification

# ── Home Assistant ─────────────────────────────────────────────────────────────
HOME_ASSISTANT_URL=http://192.168.1.100:8123
HOME_ASSISTANT_TOKEN=...
HOME_ASSISTANT_VERIFY_SSL=false

# ── Twilio / WhatsApp ──────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID=ACxxxxxxxx
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886

# ── IONOS Cloud ────────────────────────────────────────────────────────────────
IONOS_TOKEN=<bearer token>
IONOS_USERNAME=...
IONOS_PASSWORD=...
IONOS_SSH_PRIVATE_KEY=...
IONOS_SSH_PUBLIC_KEY=...

# ── MeshCentral RMM ────────────────────────────────────────────────────────────
MESHCENTRAL_URL=https://your-domain.com/rmm
MESHCENTRAL_INTERNAL_URL=http://meshcentral:4430
MESHCENTRAL_USER=admin
MESHCENTRAL_PASSWORD=<strong password>
MESHCENTRAL_DOMAIN=sentinel
MESHCENTRAL_DEFAULT_MESH_ID=...

# ── Sentry ─────────────────────────────────────────────────────────────────────
SENTRY_DSN=https://key@sentry.io/project
SENTRY_AUTH_TOKEN=...            # for auto-triage API access
SENTRY_ORG=your-org-slug
SENTRY_PROJECT=your-project-slug
SENTRY_WEBHOOK_SECRET=...        # optional — verifies inbound webhooks

# ── Reddit ─────────────────────────────────────────────────────────────────────
REDDIT_USER_AGENT=sentinel:1.0 (by u/youruser)
REDDIT_SUBREDDITS=programming,python,devops   # comma-separated default feeds

# ── Sentinel Mesh Agent Gateway ────────────────────────────────────────────────
AGENT_GATEWAY_MASTER_SECRET=<generate: openssl rand -hex 32>
AGENT_HMAC_TS_DRIFT_MAX=60       # seconds; replay attack window
AGENT_HEARTBEAT_TIMEOUT=120      # seconds before agent marked offline

# ── TELOS personal context ─────────────────────────────────────────────────────
TELOS_DIR=/root/sentinel/telos
TELOS_CACHE_TTL_SECONDS=300

# ── Observability ──────────────────────────────────────────────────────────────
LOG_LEVEL=INFO
LOG_DIR=/var/log/aibrain

# ── Cost tracking & rate limiting ──────────────────────────────────────────────
DAILY_COST_CEILING_USD=10.0
BUDGET_ALERT_THRESHOLDS=0.5,0.8,1.0
SONNET_DAILY_TOKEN_BUDGET=0      # 0 = unlimited
HAIKU_DAILY_TOKEN_BUDGET=0
RATE_LIMIT_PER_MINUTE=20
RATE_LIMIT_PER_HOUR=200

# ── Autonomy ───────────────────────────────────────────────────────────────────
BRAIN_AUTONOMY=false             # true = execute all pending actions without confirmation

# ── Memory ─────────────────────────────────────────────────────────────────────
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
QDRANT_COLLECTION=brain_memories
MEMORY_FLUSH_INTERVAL_TURNS=10

# ── Monitoring dashboards ──────────────────────────────────────────────────────
GRAFANA_USER=admin
GRAFANA_PASSWORD=<strong password>
FLOWER_USER=admin
FLOWER_PASSWORD=<strong password>

# ── Repository (Brain self-modification) ───────────────────────────────────────
REPO_LOCAL_PATH=/root/sentinel-workspace
REPO_WORKSPACE=/workspace/repo
REPO_SSH_KEY_PATH=/root/.ssh/id_ed25519

# ── CI/CD ──────────────────────────────────────────────────────────────────────
DEPLOY_WEBHOOK_SECRET=<generate: openssl rand -hex 32>

# ── Timezone ───────────────────────────────────────────────────────────────────
TIMEZONE=America/Chicago
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
scp -r . user@your-server:~/sentinel/
```

### 3. Configure environment

Fill in `.env` using the reference above.

### 4. Configure Nginx

Edit `nginx/nginx.conf` and replace `YOUR_DOMAIN` with your actual domain.

### 5. Get SSL certificates

```bash
chmod +x scripts/get_ssl.sh
./scripts/get_ssl.sh
```

### 6. Deploy

```bash
./scripts/deploy.sh
```

Builds the brain container and starts all services. Verify:

```bash
curl https://your-domain.com/api/v1/health
# → {"status":"ok","redis":true,"postgres":true}

docker compose ps
```

All containers should show `running` or `healthy`. The Celery workers may take 10–15 seconds to connect to Redis after startup.

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

# Show container status
./scripts/deploy.sh status

# Stop everything
./scripts/deploy.sh stop

# Rebuild only the brain after a code change
docker compose build --no-cache brain && docker compose up -d brain

# Rebuild Celery workers after a task change
docker compose build --no-cache celery-worker celery-worker-workspace && \
  docker compose up -d celery-worker celery-worker-workspace celery-beat flower
```

---

## Dashboards

### Grafana — Infrastructure Metrics

**URL:** `https://your-domain.com/grafana/`
**Login:** `GRAFANA_USER` / `GRAFANA_PASSWORD` from `.env`

The **Brain Overview** dashboard loads as the home dashboard. Rows include:

| Row | Panels |
|-----|--------|
| Stats | Request rate · Error rate · P95 latency · Celery workers online |
| Traffic | Request rate + errors over time · Response latency percentiles |
| LLM | Tokens per minute by model · LLM API latency |
| Celery | Requests by intent · Tasks by state over time · Queue depth |
| Redis | Hit rate over time · Connected clients |
| Cost | Daily spend vs ceiling · Accumulation with ceiling line |
| Mesh Agents | Connected agents · Heartbeat timeline · Patch history |

#### Useful PromQL queries

```promql
# P95 response latency (ms)
histogram_quantile(0.95, sum by(le) (rate(brain_response_latency_seconds_bucket[5m]))) * 1000

# LLM token consumption (per minute, by model)
sum by(model, direction) (rate(brain_llm_tokens_total[1m]) * 60)

# Today's total spend
brain_cost_usd_daily

# Error rate percentage
100 * sum(rate(brain_requests_total{success="false"}[5m]))
    / sum(rate(brain_requests_total[5m]))

# Celery task failure rate
rate(celery_tasks_total{state="FAILURE"}[5m]) / rate(celery_tasks_total[5m])
```

### Flower — Celery Task Monitor

**URL:** `https://your-domain.com/flower/`
**Login:** `FLOWER_USER` / `FLOWER_PASSWORD` (HTTP Basic Auth)

### Prometheus — Raw Metrics

Access via SSH tunnel (not exposed through Nginx):

```bash
ssh -L 9090:localhost:9090 user@your-server
# Then open: http://localhost:9090
```

### MeshCentral RMM Console

**URL:** `https://your-domain.com/rmm/`
**Login:** `MESHCENTRAL_USER` / `MESHCENTRAL_PASSWORD`

---

## TELOS — Personal Context

The `telos/` directory holds Markdown files injected into every LLM system prompt:

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

Reload without restarting:

```bash
curl -X POST https://your-domain.com/api/v1/telos/reload
# → {"reloaded": ["beliefs.md", "context.md", ...]}
```

---

## Slack Interface

The brain connects via Socket Mode — no public webhook required.

**Sending messages:** DM the bot directly, or `@Brain` mention it in any channel.

**Examples:**

```
What should I focus on this week?
Debug this Python function: [paste code]
Draft an email to sarah@company.com about the Q1 proposal
What do I have tomorrow?
What are my open GitHub issues?
Turn off the living room lights
Create a task: fix the mobile login bug, priority 4
List my open tasks
list mesh agents
show rmm devices
get top reddit posts from r/python
confirm      ← executes a pending write action
cancel       ← aborts a pending write action
```

### Codebase self-editing from Slack

```
# 1. Read a file
read app/brain/intent.py

# 2. Make a change
patch app/brain/intent.py — add routing hint for task_create

# 3. Brain shows preview, ask to confirm
confirm

# 4. Commit and push
commit these changes with message: improve task routing

# 5. Rebuild and restart
deploy
```

---

## CLI — brain.py

`brain.py` is a terminal client that connects to the running brain API and joins the shared cross-interface session.

### Running

```bash
python3 brain.py                      # REPL (joins shared "brain" session)
python3 brain.py --session NAME       # Private isolated session
python3 brain.py chat "message"       # One-shot message
```

### Subcommands

```bash
# System status
brain.py health               # Health check (Redis, Postgres)
brain.py costs                # Today's LLM spend breakdown
brain.py tasks                # Celery queue status
brain.py context              # Server/repo/Docker context snapshot
brain.py git                  # Git status + recent log
brain.py docker               # Docker ps output

# Approval workflow
brain.py pending              # List pending write tasks
brain.py approve <id>         # Approve a pending task
brain.py cancel <id>          # Cancel a pending task
brain.py level                # Show current approval level
brain.py level [1|2|3]        # Set approval level

# Sessions & tasks
brain.py sessions             # List saved sessions
brain.py mytasks [status]     # Task board (optional status filter)
brain.py history              # Show chat history
brain.py clear                # Clear session memory
```

### REPL slash commands

Inside the REPL, prefix any subcommand with `/`:

```
/health  /costs  /tasks  /context  /git  /docker
/pending  /approve <id>  /cancel <id>  /level [1|2|3]
/mytasks [status]  /history  /clear  /sessions
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BRAIN_URL` | `http://localhost:8000` | API base URL |
| `BRAIN_SESSION` | `brain` | Session ID override |
| `NO_COLOR` | — | Disable ANSI colors |

---

## REST API

Base URL: `https://your-domain.com`

Interactive docs (non-production only): `https://your-domain.com/docs`

### Endpoint reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/health` | Detailed health (Redis + Postgres) |
| `POST` | `/api/v1/chat` | Send a message, get a reply |
| `DELETE` | `/api/v1/chat/{session_id}` | Clear session history |
| `GET` | `/api/v1/agents` | List agent personas |
| `POST` | `/api/v1/telos/reload` | Reload TELOS files from disk |
| `GET` | `/api/v1/integrations/status` | Which integrations are configured |
| `GET` | `/api/v1/integrations/gmail` | List emails |
| `GET` | `/api/v1/integrations/calendar` | List calendar events |
| `GET` | `/api/v1/integrations/github/issues` | List GitHub issues |
| `GET` | `/api/v1/integrations/github/prs` | Open pull requests |
| `GET` | `/api/v1/integrations/home-assistant/states` | All HA entity states |
| `POST` | `/api/v1/integrations/home-assistant/service` | Call an HA service |
| `POST` | `/api/v1/integrations/n8n/trigger` | Trigger an n8n workflow |
| `POST` | `/api/v1/feedback/rate` | Rate a response (1–10) |
| `POST` | `/api/v1/feedback/thumbs` | Thumbs up / down |
| `GET` | `/api/v1/feedback/summary` | Aggregate rating stats |
| `GET` | `/api/v1/costs` | Today's LLM spend and remaining budget |
| `WS` | `/api/v1/observe/stream` | Real-time event stream (WebSocket) |
| `GET` | `/api/v1/observe/metrics` | Aggregate performance metrics |
| `GET` | `/api/v1/observe/events` | Recent event buffer |
| `GET` | `/api/v1/board/tasks` | List task board tasks |
| `POST` | `/api/v1/board/tasks` | Create a task |
| `GET` | `/api/v1/board/tasks/{id}` | Get single task |
| `PATCH` | `/api/v1/board/tasks/{id}` | Update a task |
| `DELETE` | `/api/v1/board/tasks/{id}` | Soft-delete (cancel) a task |
| `POST` | `/api/v1/board/tasks/archive-done` | Archive all done tasks |
| `DELETE` | `/api/v1/board/tasks/{id}/purge` | Hard-delete a task |
| `GET` | `/api/v1/board/activity` | Live AI activity feed |
| `POST` | `/api/v1/agents/provision` | Provision a new mesh agent |
| `GET` | `/api/v1/agents/` | List mesh agents |
| `GET` | `/api/v1/agents/{id}/health` | Agent info + latest heartbeat |
| `GET` | `/api/v1/agents/{id}/patches` | Agent patch history |
| `POST` | `/api/v1/agents/{id}/revoke` | Revoke agent credentials |
| `WS` | `/ws/agent/{agent_id}` | Mesh agent WebSocket connection |
| `POST` | `/api/v1/sentry/webhook` | Receive Sentry issue webhooks |
| `GET` | `/api/v1/sentry/issues` | List tracked Sentry issues |
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
```

### Task Board

```bash
# List open tasks
curl "https://your-domain.com/api/v1/board/tasks?status=pending&limit=20"

# Create a task
curl -X POST https://your-domain.com/api/v1/board/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "Fix login bug", "priority": 4, "approval_level": 1}'

# Live AI activity feed
curl https://your-domain.com/api/v1/board/activity
```

Task fields: `title`, `description`, `status` (pending|in_progress|done|cancelled|failed|archived), `priority` (1–5), `approval_level` (1–3), `due_date`, `source`, `tags`, `assigned_to`, `blocked_by` (dependency list of task IDs).

### Costs

```bash
curl https://your-domain.com/api/v1/costs
# → date, total_cost_usd, daily_ceiling_usd, pct_of_ceiling, remaining_usd, models breakdown
```

---

## Intent Routing & Agent Personas

Every message is classified by Claude Haiku into an intent. The intent selects both the skill to execute and the agent persona to respond as.

### Agent personas

| Agent | Strengths | Model | Max tokens |
|-------|-----------|-------|-----------|
| **Engineer** | Code, architecture, GitHub, shell | Sonnet | 8096 |
| **Writer** | Drafts, emails, long-form | Sonnet | 4096 |
| **Researcher** | Analysis, comparisons, TL;DR-first | Sonnet | 4096 |
| **Strategist** | Decisions, frameworks, go-to-market | Sonnet | 4096 |
| **Marketing** | Platform-native copy, AIDA/PAS/BAB, 3 variations | Sonnet | 6000 |
| **Default** | General assistant (Brain persona) | Sonnet | 2048 |

---

## Skills Reference

43 registered skills across all domains:

**Communication**
`chat` · `gmail_read` · `gmail_send` · `gmail_reply` · `whatsapp_read` · `whatsapp_send` · `contacts_read` · `contacts_write` · `slack_read`

**Productivity**
`calendar_read` · `calendar_write` · `task_create` · `task_read` · `task_update`

**Code & Repositories**
`github_read` · `github_write` · `repo_read` · `repo_write` · `repo_commit` · `code`

**Infrastructure & Automation**
`server_shell` · `deploy` · `n8n_execute` · `n8n_manage` · `cicd_read` · `cicd_trigger` · `cicd_debug` · `ionos_cloud` · `ionos_dns` · `smart_home`

**Content Creation**
`content_draft` · `social_caption` · `ad_copy` · `content_repurpose` · `content_calendar`

**Knowledge & Research**
`research` · `deep_research` · `knowledge_graph` · `data_intelligence` · `architecture_advisor`

**Monitoring & Observability**
`sentry_read` · `sentry_manage` · `bug_hunter` · `rmm_read` · `rmm_manage`

**Mesh Agent System**
`agent_registry` · `agent_manage` · `remote_log` · `patch_dispatch` · `cross_agent_context`

**Feeds**
`reddit_read` · `reddit_schedule`

**Meta**
`skill_discovery`

Write actions (email send, calendar write, file edits, git push, shell commands, etc.) require `confirm` by default. Set `BRAIN_AUTONOMY=true` to skip all confirmations.

---

## Task Board

The task board is the brain's persistent work queue. Tasks are created automatically by skills (Sentry triage, bug hunter, skill discovery) or manually via Slack/CLI/API.

### Approval levels

| Level | Behavior |
|-------|----------|
| 1 | Auto-starts immediately (no confirmation) |
| 2 | Queued; owner receives DM approval request |
| 3 | Blocked until explicit CLI/Slack `confirm` |

### Dependency chaining

Tasks support `blocked_by` — a list of task IDs that must reach `done` before this task can start. The dispatcher auto-unblocks dependent tasks when a dependency completes.

### Milestone logging

Every confirmed write action (email, git commit, file edit, shell command, deploy, DNS change, etc.) is logged to the `ai_milestones` table and posted to `#sentinel-milestones` in Slack.

---

## Sentinel Mesh Agent System

Remote servers running the Sentinel Agent daemon connect to the brain over a HMAC-SHA256 signed WebSocket. The brain receives continuous telemetry and can dispatch code patches.

See `sentinel-agent/README.md` for installation instructions.

### How it works

```
Remote server (Sentinel Agent daemon)
       │
       │  wss://your-domain.com/ws/agent/{id}
       │  HMAC-SHA256 signed, 60s replay window
       ▼
Brain WS Gateway (/ws/agent/{id})
       ├── Writes inbound messages to Redis stream (sentinel:agents:stream)
       ├── Celery: agent-stream-consumer (1 min) → parses + stores in DB
       ├── Celery: agent-heartbeat-monitor (2 min) → alerts on offline agents
       └── Skills: AgentRegistry, AgentManage, RemoteLog, PatchDispatch
```

### Provisioning an agent

```bash
# Via Slack
list mesh agents

# Via CLI
python3 brain.py chat "provision new agent app_name=my-app env=staging"
# → returns AGENT_ID and AGENT_TOKEN (shown once)
```

### Patch workflow

1. Brain detects an issue (Sentry triage, bug hunter, or user request)
2. Brain dispatches patch to agent via Redis (`sentinel:agent:cmd:{id}`)
3. Agent snapshots current state, applies diff, runs tests
4. **Production agents:** Slack approval required before patch is dispatched
5. Result (success/failure + logs) reported back over WebSocket

### Redis keys

| Key | Purpose |
|-----|---------|
| `sentinel:agents:stream` | Inbound telemetry stream (all agents) |
| `sentinel:agent:cmd:{id}` | Outbound command queue (per agent) |
| `sentinel:agent:patch_approval:{id}` | Pending patch approval state |

---

## RMM — MeshCentral Integration

MeshCentral is deployed as a container and provides remote monitoring and management of all servers. The brain integrates with it via REST API and WebSocket event stream.

### Access

- **Console:** `https://your-domain.com/rmm/`
- **Agent connections:** direct via `/agent.ashx` and `/meshrelay.ashx` (bypasses Nginx buffering)

### Celery background tasks

| Task | Schedule | What it does |
|------|----------|-------------|
| `rmm-device-poll` | Every 60s | Check device online/offline status, alert Slack on change |
| `rmm-full-sync` | Every 5 min | Full inventory sync (hostname, OS, IP, agent version) |
| `rmm-incident-check` | Every 2 min | Threshold breach detection (CPU/mem/disk/offline) |

### Slack channels

- Production issues → `#rmm-production`
- Dev/staging issues → `#rmm-dev-staging`

### Device groups

- Sentinel Infrastructure
- Production
- Dev-Staging

---

## Reddit News-Feed

The brain can fetch, summarize, and schedule Reddit digests.

### Slack commands

```
get top posts from r/python
summarize r/devops today
schedule daily digest from r/programming at 9am
```

### Configuration

```bash
REDDIT_USER_AGENT=sentinel:1.0 (by u/youruser)
REDDIT_SUBREDDITS=programming,python,devops
SLACK_REDDIT_CHANNEL=sentinel-reddit
```

### Scheduled digests

The `reddit-digest-dispatch` Celery job runs hourly and fires any scheduled digests that are due. Create a schedule via Slack:

```
schedule weekly reddit digest from r/MachineLearning every Monday at 8am
```

---

## Sentry Auto-Triage

The brain automatically ingests, investigates, and attempts to fix Sentry errors on a schedule. No manual triage required.

### Pipeline

1. **Ingest** (4x daily: 00:00, 06:00, 12:00, 18:00 UTC) — Fetch top 10 errors by frequency from the past 6 hours
2. **Task creation** — One board task per error (approval_level=1, auto-starts)
3. **Investigation** — Fetch full Sentry event (stack trace, breadcrumbs, source context); LLM produces `fix_plan` JSON
4. **Fix** — Creates `sentinel/sentry-{issue_id}` branch, patches files, commits, pushes, opens PR
5. **Report** — Posts root cause + PR link + files changed to `#sentinel-alerts`
6. **Resolve** — Marks issue resolved in Sentry if fix is complete

The owner only needs to review and merge the GitHub PR.

### Inbound webhooks

Sentry can also push issues in real time:

```
POST https://your-domain.com/api/v1/sentry/webhook
```

Configure in Sentry: **Settings → Integrations → Webhooks → Add → your URL**.

### Severity → action mapping

| Level | Approval | Action |
|-------|----------|--------|
| fatal | BREAKING | Always requires confirmation |
| critical / error | CRITICAL | Requires Slack approval |
| warning | STANDARD | Task created, auto-queued |
| info / debug | — | Logged only |

---

## Cost Tracking & Rate Limiting

Every LLM call is metered in real time.

### How it works

```
Incoming message
  │
  ▼ RateLimiter.check(session_id)      Redis INCR + TTL
  │  → over limit: return ⏱️ reply immediately
  │
  ▼ CostTracker.check_budget(model)    Redis GET
  │  → over ceiling: return ⚠️ reply, no API call
  │
  ▼ Anthropic API call
  │
  ▼ CostTracker.record(model, in, out)
       atomic Redis pipeline:
         INCRBYFLOAT brain:cost:daily:{date}:total
         INCRBY      brain:cost:daily:{date}:model:{model}:tokens_*
       → threshold check → Slack alert
       → Prometheus gauges updated
```

### Pricing

| Model | Input | Output |
|-------|-------|--------|
| `claude-sonnet-4-6` | $3.00 / 1M tokens | $15.00 / 1M tokens |
| `claude-haiku-4-5-20251001` | $0.25 / 1M tokens | $1.25 / 1M tokens |

Update `PRICING` in `app/brain/cost_tracker.py` when Anthropic changes rates.

---

## Memory System

Three tiers work together automatically:

| Tier | Storage | Contents | Lifetime |
|------|---------|----------|----------|
| **Hot** | Redis `brain:session:*` | Last 20 turns | 4 hours TTL |
| **Warm** | Postgres `session_summaries` | Haiku-generated summary, every 10 turns | Permanent |
| **Cold** | Qdrant `brain_memories` | Semantic embeddings of high-signal turns (>200 chars) + rated ≥ 8 | Permanent |
| **Graph** | Neo4j | Entity relationships, knowledge links | Permanent |

All four tiers are fetched in parallel on each request and prepended to the LLM prompt.

---

## Cross-Interface Memory

All three interfaces (Slack, CLI, REST) default to the shared `brain` primary session. Memory and context persist across interfaces — a conversation started in Slack continues seamlessly in the CLI.

- **Slack:** per-user session (`slack:{user_id}`), cross-posts to primary
- **CLI:** always joins `brain` primary session; `--session NAME` for private
- **REST:** empty/default `session_id` maps to `brain` primary

To use a private session from the CLI:

```bash
python3 brain.py --session private-work
```

---

## Autonomy Mode

When `BRAIN_AUTONOMY=true`, all pending write actions execute immediately without asking for `confirm`. The LLM prompt includes a `[FULL AUTONOMY MODE]` instruction.

```bash
# Enable
echo "BRAIN_AUTONOMY=true" >> .env
docker compose up -d brain

# Disable
# Set BRAIN_AUTONOMY=false and restart
```

Approval levels still apply to task board tasks — `approval_level=2` tasks DM the owner even in autonomy mode.

---

## Task Queue — Celery + Flower

Three task workers handle all background work:

| Container | Queues | Concurrency |
|-----------|--------|-------------|
| `celery-worker` | evals, celery, tasks_general | 3 |
| `celery-worker-workspace` | tasks_workspace | 1 (serialized git ops) |
| `celery-beat` | — (scheduler only) | — |

### Scheduled jobs (15 total)

| Task | Schedule | What it does |
|------|----------|-------------|
| `weekly-agent-evals` | Sun 09:00 UTC | Agent quality scorecard → Slack |
| `nightly-integration-evals` | Daily 02:00 UTC | Read-only integration health checks → Slack |
| `health-check` | Every 30 min | Brain/Redis/Postgres health → Slack on failure |
| `scan-pending-tasks` | Every 1 min | Dispatch pending board tasks to workers |
| `aggregate-error-metrics` | Hourly | Aggregate error buffer metrics |
| `sentry-error-triage` | 4x daily (0,6,12,18 UTC) | Fetch top Sentry errors, investigate + PR |
| `autonomous-bug-hunt` | Every 6h at :30 | Cluster errors, LLM root-cause, create fix tasks |
| `poll-sentinel-prs` | Every 15 min | Poll GitHub for sentinel/* PRs, trigger review |
| `rmm-device-poll` | Every 60s | Device online/offline status → Slack on change |
| `rmm-full-sync` | Every 5 min | Full RMM inventory sync |
| `rmm-incident-check` | Every 2 min | CPU/mem/disk/offline threshold detection |
| `reddit-digest-dispatch` | Hourly at :00 | Fire due Reddit digest schedules |
| `agent-heartbeat-monitor` | Every 2 min | Detect offline mesh agents, alert Slack |
| `agent-stream-consumer` | Every 1 min | Process inbound mesh agent telemetry stream |
| `agent-heartbeat-purge` | Daily 03:00 UTC | Purge old heartbeat rows |

### Celery broker layout

| Redis DB | Purpose |
|----------|---------|
| DB 0 | App hot memory (`brain:session:*`, `brain:cost:*`, `brain:rate:*`, agent streams) |
| DB 1 | Celery broker (task queue) |
| DB 2 | Celery result backend (24hr TTL) |

### Manually trigger a scheduled job

```bash
docker compose exec celery-worker \
  celery -A app.worker.celery_app call app.worker.tasks.run_weekly_agent_evals

docker compose exec celery-worker \
  celery -A app.worker.celery_app inspect active
```

---

## Observability

### Structured logging (Loguru)

Every request emits four log lines:

```
REQUEST  | session=abc | src=slack | msg=What do I have tomorrow?...
SKILL    | calendar_read | ctx=True | 12ms
LLM      | model=claude-sonnet-4-6 | in=1240 | out=312 | 1840ms
RESPONSE | session=abc | intent=calendar_read | agent=default | 1852ms
```

Logs ship to Loki via Promtail for querying in Grafana.

### Real-time event stream (WebSocket)

```bash
wscat -c wss://your-domain.com/api/v1/observe/stream
```

Events per request: `request_received` → `skill_dispatched` → `llm_called` → `response_delivered`

### Prometheus metrics

**Brain metrics** (scraped from `/metrics`):

| Metric | Type | Description |
|--------|------|-------------|
| `brain_requests_total` | Counter | Total requests (`intent`, `agent`, `success`) |
| `brain_response_latency_seconds` | Histogram | End-to-end latency |
| `brain_llm_tokens_total` | Counter | Tokens consumed (`model`, `direction`) |
| `brain_llm_latency_seconds` | Histogram | LLM API round-trip |
| `brain_skill_duration_seconds` | Histogram | Per-skill execution time |
| `brain_cost_usd_daily` | Gauge | Today's LLM spend |
| `brain_cost_ceiling_usd` | Gauge | Configured daily ceiling |
| `brain_budget_exceeded_total` | Counter | Calls blocked by budget |
| `brain_rate_limited_total` | Counter | Requests blocked by rate limiter |

**Infrastructure metrics** from exporters: `redis_memory_used_bytes`, `redis_connected_clients`, `celery_tasks_total{state}`, `celery_queue_length`, `pg_stat_activity_count`, container CPU/mem from cAdvisor, host CPU/mem/disk from node-exporter.

### Sentry error tracking

```bash
SENTRY_DSN=https://your-key@sentry.io/your-project-id
```

Captures skill failures, LLM errors, and unhandled exceptions. Tagged with `intent`, `agent`, `session_id`. `WebSocketDisconnect` and `CancelledError` are filtered as noise.

---

## Eval System

### Schedules

| Job | When | What happens |
|-----|------|-------------|
| Agent quality evals | Sunday 09:00 UTC | 3 test cases × 6 agents, Haiku as judge, Slack scorecard |
| Integration reliability | 02:00 UTC nightly | Read-only checks of Gmail, Calendar, GitHub, n8n, Home Assistant |

### Run manually

```bash
python3 evals/run_evals.py
python3 evals/run_evals.py --agent engineer
python3 evals/run_evals.py --nightly --slack
```

### Adding a test case

Create `evals/agents/<agent>/test_XX_name.json`:

```json
{
  "input": "The prompt sent to the agent",
  "criteria": ["what the response must include"],
  "judge_prompt": "Context telling the Haiku judge what a good answer looks like.",
  "threshold": 7
}
```

---

## Security

The `SecurityHook` runs before every message and blocks:

- Injection tokens: `[INST]`, `<<SYS>>`, `<|im_start|>`
- "ignore previous instructions" and similar phrases
- DAN / jailbreak phrases
- System prompt exfiltration attempts
- Identity confusion ("you are now", "pretend to be")

Pattern list: `app/security/patterns.py`

**Repository safety:**
- `.env` and secrets files are blocked from commits by `repo.py`
- `_scan_secrets()` aborts push if secrets patterns are detected
- Protected branch `main` — all changes via PR

**Mesh Agent security:**
- HMAC-SHA256 signed messages with 60s replay window
- Agent tokens stored as SHA-256 hash (never plaintext)
- Production patches require Slack approval

---

## Google OAuth Setup

```bash
python3 scripts/google_auth.py
```

Follow the browser prompt. Paste the refresh token into `.env` as `GOOGLE_REFRESH_TOKEN`. For additional accounts, use the same script and set `GOOGLE_ACCOUNT_2_REFRESH_TOKEN`, etc.

---

## Updating

```bash
git pull
docker compose build --no-cache brain celery-worker celery-worker-workspace celery-beat
docker compose up -d
```

Database schema changes in `app/db/schema.sql` run automatically on startup — all `CREATE TABLE` statements use `IF NOT EXISTS`.

---

## CI/CD — GitOps Loop

The brain can edit its own code, open a PR, and deploy automatically after CI passes.
Full setup: **[CICD.md](./CICD.md)**

Quick summary:
1. Add `DEPLOY_WEBHOOK_SECRET` to GitHub repo secrets and to `.env`
2. `docker compose up -d deploy-agent && docker compose restart nginx`
3. Push to `main` → CI runs → image pushed to GHCR → webhook fires → brain restarts
4. Make the GHCR package **Public** after the first image push
5. Set branch protection so all changes go through PRs

---

## Troubleshooting

**Brain container exits on startup**
```bash
docker compose logs brain
```
Usually a missing `.env` value or Postgres not yet healthy.

**Slack bot not responding**
- Verify `SLACK_APP_TOKEN` starts with `xapp-` (Socket Mode, not webhook)
- Check: `docker compose logs brain | grep -i slack`

**Gmail / Calendar returning "not configured"**
- Confirm `GOOGLE_REFRESH_TOKEN` is in `.env`
- Re-run `python3 scripts/google_auth.py` if expired

**Qdrant embeddings are zero vectors**
- Add `OPENAI_API_KEY` to `.env`

**Grafana shows "No data"**
- SSH tunnel to Prometheus: `ssh -L 9090:localhost:9090 user@server`
- Check targets at `http://localhost:9090/targets` — all should be `UP`

**Mesh agent not connecting**
- Verify `AGENT_ID` and `AGENT_TOKEN` are correct
- Check system clock sync (`timedatectl`) — max 60s drift
- Verify firewall allows 443 outbound from agent server

**MeshCentral agents not registering**
- Check `extra_hosts` in docker-compose.yml points to correct nginx IP
- Verify `/agent.ashx` and `/meshrelay.ashx` nginx locations exist
- Agents connect directly (not via `/rmm/`), check nginx logs

**Celery tasks not running on schedule**
- `docker compose ps celery-beat` — must be running
- `docker compose logs celery-beat`
- `docker compose exec celery-beat celery -A app.worker.celery_app inspect scheduled`

**Budget ceiling hit — brain is blocked**
- `curl https://your-domain.com/api/v1/costs`
- Raise `DAILY_COST_CEILING_USD` in `.env` (takes effect instantly, no restart)
- Or clear the Redis counter manually:
  ```bash
  docker compose exec redis redis-cli -a "$REDIS_PASSWORD" \
    DEL "brain:cost:daily:$(date +%Y-%m-%d):total"
  ```

**Flower shows no workers**
- Workers need 10–15s after startup to connect
- `docker compose logs celery-worker | grep -i "ready"`

**Reset a session**
```bash
curl -X DELETE https://your-domain.com/api/v1/chat/your-session-id
```
