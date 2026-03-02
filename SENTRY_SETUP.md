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

Open `.env` and fill in the three Sentry values:

```env
# ── Observability ─────────────────────────────────────────
SENTRY_DSN=https://abc123xyz@o123456.ingest.sentry.io/7654321
SENTRY_AUTH_TOKEN=sntrys_your_token_here
SENTRY_ORG=your-org-slug
SENTRY_PROJECT=ai-brain
```

> `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, and `SENTRY_PROJECT` are only used by
> Grafana's Sentry datasource — they are not read by the Brain app itself.
> The Brain app only reads `SENTRY_DSN`.

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

## Alerting (Optional)

Once errors are flowing, set up Sentry alerts so you get notified without
watching the dashboard:

1. Go to **Alerts → Create Alert Rule**
2. Recommended rule: **"Notify on first occurrence of any new issue"**
3. Add a Slack notification action — connect Sentry to your Slack workspace via
   **Settings → Integrations → Slack**
4. Point it at your `sentinel-alerts` channel

This gives you a Slack ping the moment a new error type hits production — before
any user reports it.

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
