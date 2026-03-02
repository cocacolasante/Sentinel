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
