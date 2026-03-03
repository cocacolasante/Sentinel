# Sentry Setup — AI Brain Error Tracking

Sentry gives you real-time error tracking, stack traces, and performance data for
the Brain API. The SDK is already wired into the app — you just need a Sentry
project and the right keys.

---

## How It Works in This Stack

```
Brain API (FastAPI)
  └── sentry-sdk  →  sentry.io  (captures exceptions + performance traces)

Grafana Dashboard (Errors row)
  └── grafana-sentry-datasource  →  sentry.io  (queries issues, error rates)
```

Both connections use separate credentials:
- **Brain → Sentry**: DSN (a URL, identifies your project)
- **Grafana → Sentry**: Auth Token (a personal API token with read access)

---

## Step 1 — Create a Sentry Account

1. Go to **[sentry.io](https://sentry.io)** and sign up (free tier is sufficient)
2. When prompted to create an organization, use `csuite-code` or whatever name you prefer
3. Keep your **organization slug** handy — you'll need it for Grafana
   (shown in the URL: `https://sentry.io/organizations/<your-slug>/`)

---

## Step 2 — Create a Project

1. In Sentry, go to **Projects → Create Project**
2. Select platform: **Python → FastAPI** (or just Python)
3. Set the project name: `ai-brain`
4. Click **Create Project**
5. Sentry will show you a DSN — it looks like:
   ```
   https://abc123xyz@o123456.ingest.sentry.io/7654321
   ```
   Copy this — you'll add it to `.env` in Step 4.

---

## Step 3 — Create an Auth Token (for Grafana)

1. Go to **Settings → Account → API → Auth Tokens**
   Direct URL: `https://sentry.io/settings/account/api/auth-tokens/`
2. Click **Create New Token**
3. Name it: `grafana-read`
4. Select these scopes:
   - `project:read`
   - `org:read`
   - `event:read`
5. Click **Create Token** and copy the token — you'll only see it once

---

## Step 4 — Add to `.env`

Open `.env` and fill in the Sentry values:

```env
# ── Observability / Sentry ────────────────────────────────
SENTRY_DSN=https://abc123xyz@o123456.ingest.sentry.io/7654321
SENTRY_AUTH_TOKEN=sntrys_your_token_here   # used by Brain skill + Grafana
SENTRY_ORG=your-org-slug
SENTRY_PROJECT=ai-brain
SENTRY_WEBHOOK_SECRET=                     # optional — see Step 9 below
```

> **`SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, and `SENTRY_PROJECT`** are used by
> both Grafana's Sentry datasource **and** the Brain's `sentry_read` /
> `sentry_manage` skills. Without them the Brain can still receive webhook
> alerts and create tasks, but it won't be able to query or manage issues
> via natural language.
>
> **`SENTRY_DSN`** is used only by the Brain SDK for sending errors to Sentry.

---

## Step 5 — Rebuild and Restart the Brain

After adding `SENTRY_DSN` to `.env`, rebuild so the app picks it up:

**Local:**
```bash
docker compose up --build -d brain
docker compose logs -f brain | grep -i sentry
```

You should see:
```
ai-brain | Sentry initialised | env=development
```

If you see `Sentry DSN not set`, the value wasn't loaded — double-check `.env`.

**Production (Ubuntu server):**
```bash
cd ~/ai-brain
docker compose up --build -d brain
docker compose logs -f brain | grep -i sentry
```

---

## Step 6 — Verify Errors Are Reaching Sentry

### Option A — Trigger a test error via the API

```bash
# This endpoint doesn't exist — will generate a 404 event in Sentry
curl http://localhost:8000/api/v1/nonexistent-test-endpoint
```

Then go to Sentry → **Issues** — you should see a new issue appear within 30 seconds.

### Option B — Use Sentry's built-in test

In Sentry, go to your project → **Settings → Client Keys (DSN)** → **Send Test Event**.

---

## Step 7 — Add Sentry as a Grafana Data Source

This connects the Grafana dashboard's **Errors** row to your Sentry project.

### Prerequisite

The `grafana-sentry-datasource` plugin installs automatically on container start
(configured via `GF_INSTALL_PLUGINS` in `docker-compose.yml`). Verify it's installed:

1. Open Grafana at `http://localhost:3000` (local) or `https://your-domain.com/grafana/`
2. Go to **Administration → Plugins**
3. Search for "Sentry" — it should show as **Installed**

If it shows as not installed, check `docker compose logs grafana | grep sentry`.

### Add the Data Source

1. Go to **Connections → Data Sources → Add new data source**
2. Search for **Sentry** and click it
3. Fill in the fields:

| Field | Value |
|-------|-------|
| Name | `Sentry` |
| Auth Token | your `SENTRY_AUTH_TOKEN` |
| Organization | your org slug (e.g. `csuite-code`) |
| Project | `ai-brain` |

4. Click **Save & Test** — should show **Data source connected**

> The datasource UID must be `sentry` for the dashboard panels to pick it up.
> After saving, go to the datasource settings page — the UID is shown in the URL:
> `http://localhost:3000/datasources/edit/<uid>`. If it's not `sentry`, edit the
> field manually before saving.

---

## Step 8 — Verify the Grafana Errors Row

1. Open the **AI Brain — Single Pane of Glass** dashboard
2. Scroll to **Row 4 — Errors (Sentry)**
3. The panels should now show:
   - **Unresolved Issues** — count of open issues
   - **Error Rate Trend** — 7-day chart
   - **Top Errors by Frequency** — table of most frequent errors

---

## Local vs Production Differences

| | Local | Production |
|---|---|---|
| Sentry account | Same `sentry.io` account | Same `sentry.io` account |
| DSN | Same DSN | Same DSN |
| Environment tag | `development` | `production` |
| Errors appear in Sentry | Yes | Yes |
| Grafana datasource | Manual setup via UI (same steps) | Manual setup via UI (same steps) |

The environment tag (`development` vs `production`) is automatically set from
`ENVIRONMENT` in `.env` — use it in Sentry's issue filters to separate local
noise from real production errors:

> **Sentry → Issues → Filter: `environment:production`**

---

## Step 9 — Connect Sentry Webhooks to the Brain (Task Integration)

This is the most powerful part: when Sentry detects an error, it pushes a
webhook to the Brain and the Brain automatically creates an approval task,
classified by severity.

### How severity maps to the approval system

| Sentry Level | Approval Category | Behavior at default level (1) |
|---|---|---|
| `fatal` | BREAKING | Always requires confirmation |
| `critical` / `error` | CRITICAL | Requires confirmation at levels 1 & 2 |
| `warning` | STANDARD | Requires confirmation at level 1 only |
| `info` / `debug` | NONE | Logged silently — no task created |

### Step 9a — Create a Sentry Internal Integration

1. In Sentry, go to **Settings → Integrations**
2. Click **Create New Integration** → **Internal Integration**
3. Name it `Brain Task Router` (or anything descriptive)
4. Under **Webhook URL**, enter:
   ```
   https://your-domain.com/api/v1/sentry/webhook
   ```
5. Under **Subscriptions**, check **Issue**
6. *(Optional)* Under **Credentials**, copy the **Client Secret** — this is your
   `SENTRY_WEBHOOK_SECRET` for signature verification

### Step 9b — Add the webhook secret to `.env`

```env
SENTRY_WEBHOOK_SECRET=your_client_secret_from_sentry
```

Leave blank to skip signature verification (only safe behind Nginx auth or if
the endpoint is not publicly exposed).

Rebuild and restart:
```bash
docker compose up --build -d brain
```

### Step 9c — Verify it's working

1. In Sentry, go to your Internal Integration → **Send Test** (or wait for a
   real error to occur)
2. Check the Brain's approval queue:
   ```bash
   curl http://localhost:8000/api/v1/approval/pending
   ```
   You should see a new task with `"action": "sentry_investigate"` and
   `"category": "critical"` (or whatever level the test event was).
3. Check the tracked issue log:
   ```bash
   curl http://localhost:8000/api/v1/sentry/issues
   ```

### Step 9d — Approve a task and get an analysis

Once a task appears in the approval queue, approve it via the API:
```bash
curl -X POST http://localhost:8000/api/v1/approval/approve/<task_id>
```

Or just tell the Brain in Slack/chat:
```
confirm
```

The Brain will fetch the full issue from Sentry, run an LLM analysis, and
return:
- Likely root cause
- Immediate mitigation steps
- Recommended fix
- Suggested next action (resolve, create GitHub issue, assign, etc.)

---

## Step 10 — Brain Commands for Sentry

Once `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, and `SENTRY_PROJECT` are set, the
Brain can read and manage Sentry issues directly via natural language:

| What you say | What happens |
|---|---|
| "show my Sentry errors" | Lists unresolved issues from the Sentry API |
| "show me Sentry issue 12345" | Fetches full detail for that issue |
| "show tracked Sentry issues" | Lists issues stored in the Brain's DB (from webhooks) |
| "resolve Sentry issue 12345" | Marks resolved (CRITICAL — asks for confirmation) |
| "ignore Sentry issue 12345" | Marks ignored (CRITICAL — asks for confirmation) |
| "assign Sentry issue 12345 to me" | Assigns to a user (STANDARD — asks at level 1) |
| "add a note to Sentry issue 12345: looking into this" | Adds a comment |

---

## Alerting (Optional — Sentry → Slack Direct)

As an alternative or complement to the Brain webhook integration, set up
Sentry's native Slack alerts so you get pinged on critical errors without
waiting for approval:

1. Go to **Alerts → Create Alert Rule**
2. Recommended rule: **"Notify on first occurrence of any new issue"**
3. Add a Slack notification action — connect Sentry to your Slack workspace via
   **Settings → Integrations → Slack**
4. Point it at your `brain-alerts` channel

The Brain webhook integration and Sentry's native Slack alerts work in parallel
— Sentry Slack pings you immediately, the Brain webhook queues an actionable
task you can approve from chat.

---

## Troubleshooting

### `Sentry DSN not set — error tracking disabled`
The `SENTRY_DSN` value in `.env` is empty or not being loaded.
```bash
docker compose exec brain env | grep SENTRY_DSN
```

### `Sentry init failed (non-fatal): ...`
The DSN format is wrong. It must start with `https://` and contain `@` and `.ingest.sentry.io`.

### Grafana shows "Datasource sentry was not found"
The datasource was not added or the UID doesn't match. In Grafana:
- Go to the datasource you added → check the UID in the URL bar
- If it's not `sentry`, update it and save

### Events not appearing in Sentry
- Check `docker compose logs brain | grep -i sentry`
- Sentry rate-limits free tier — 5,000 events/month. Check your quota at
  **Settings → Subscription**
- The app filters `WebSocketDisconnect`, `CancelledError`, and `ClientDisconnect`
  errors intentionally — these won't appear in Sentry
