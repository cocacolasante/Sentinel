# Sentinel Mesh Agent — Setup & Operations Guide

## 1. Overview

Sentinel Mesh Agents extend the central Sentinel Brain to remote project servers. Each agent is a lightweight Python daemon that relays telemetry, log analysis, and patch instructions back through a secure WebSocket channel.

**Agents are thin** — no local LLM, no vector store. All reasoning happens in the Brain.

```
┌──────────────────────────────────────────────────────────────┐
│                    SENTINEL BRAIN (Central)                  │
│  pgvector  •  Redis  •  Skill Dispatcher  •  LLM Gateway    │
│  AgentRegistry  •  PatchDispatch  •  CrossAgentContext       │
└───────────┬──────────────────────────────┬───────────────────┘
            │ WSS :443                      │ WSS :443
 ┌──────────▼──────────┐        ┌──────────▼──────────┐
 │  SENTINEL AGENT     │        │  SENTINEL AGENT     │
 │  project-server-01  │        │  project-server-02  │
 │  app: api-service   │        │  app: web-frontend  │
 └─────────────────────┘        └─────────────────────┘
```

---

## 2. Prerequisites

- Ubuntu 20.04+ (or any systemd Linux)
- Python 3.11+
- `git` installed (`apt install git`)
- Root / sudo access
- Outbound HTTPS/WSS access to `sentinelai.cloud:443`

---

## 3. Provision an Agent

Before installing, provision credentials from the Brain:

```bash
curl -s -X POST https://sentinelai.cloud/api/v1/agents/provision \
  -H "Content-Type: application/json" \
  -d '{"app_name": "my-app", "sentinel_env": "staging", "hostname": "server-01"}' \
  | jq .
```

This returns `agent_id` and `agent_token`. Keep `agent_token` — it is not shown again.

---

## 4. One-Liner Install

### Standard Python App (systemd service)

```bash
curl -fsSL https://raw.githubusercontent.com/cocacolasante/Sentinel/main/sentinel-agent/install.sh | \
  AGENT_ID="your-agent-id" \
  AGENT_TOKEN="your-agent-token" \
  BRAIN_URL="wss://sentinelai.cloud/ws/agent" \
  APP_NAME="my-app" \
  APP_DIR="/opt/my-app" \
  APP_PROCESS_NAME="uvicorn" \
  APP_HEALTH_URL="http://localhost:8000/health" \
  APP_LOG_PATH="/var/log/my-app/app.log" \
  APP_RESTART_CMD="systemctl restart my-app" \
  SENTINEL_ENV="staging" \
  bash
```

### Docker Compose App

```bash
curl -fsSL https://raw.githubusercontent.com/cocacolasante/Sentinel/main/sentinel-agent/install.sh | \
  AGENT_ID="your-agent-id" \
  AGENT_TOKEN="your-agent-token" \
  BRAIN_URL="wss://sentinelai.cloud/ws/agent" \
  APP_NAME="fluentica-ai" \
  APP_DIR="/root/ailanguagetutor" \
  APP_PROCESS_NAME="python" \
  APP_HEALTH_URL="http://localhost:8000/health" \
  APP_LOG_PATH="/var/log/my-app/app.log" \
  APP_RESTART_CMD="cd /root/ailanguagetutor && docker compose up --build -d" \
  SENTINEL_ENV="production" \
  bash
```

> **Docker notes:**
> - `APP_PROCESS_NAME` — use the process name visible inside the container, e.g. `python`, `uvicorn`, `node`, `gunicorn`. Run `docker compose top` to confirm.
> - `APP_RESTART_CMD` — use **absolute paths**. `~` does not expand in systemd environment files. Write `/root/myapp`, not `~/myapp`.
> - `APP_LOG_PATH` — use a bind-mounted host path. If logs are inside the container only, set this to empty and the agent will rely on process/HTTP monitoring.

---

## 5. Configuration Reference

Edit `/etc/sentinel-agent/env` then restart: `systemctl restart sentinel-agent`

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENT_ID` | ✅ | — | UUID returned by `/provision` |
| `AGENT_TOKEN` | ✅ | — | HMAC secret returned by `/provision` |
| `BRAIN_URL` | ✅ | `wss://sentinelai.cloud/ws/agent` | WebSocket endpoint of the Brain |
| `APP_NAME` | ✅ | `app` | Logical name for the app (used as pgvector namespace key) |
| `APP_DIR` | ✅ | `/opt/app` | Absolute path to the app directory (must be a git repo for patch dispatch) |
| `APP_PROCESS_NAME` | ✅ | `python` | Process name as seen in `ps aux` / `docker compose top` |
| `APP_HEALTH_URL` | optional | — | HTTP URL polled every 60 s for health check |
| `APP_LOG_PATH` | optional | — | Absolute path to log file for live error tailing |
| `APP_RESTART_CMD` | optional | — | Shell command to restart the app. **Must use absolute paths.** |
| `APP_TEST_CMD` | optional | — | Shell command run after patching to verify the fix |
| `SENTINEL_ENV` | optional | `staging` | `staging` = auto-patch. `production` = Slack approval required. |
| `HEARTBEAT_INTERVAL` | optional | `30` | Seconds between heartbeat pings |

### Docker Compose — recommended values

```ini
APP_PROCESS_NAME=python          # or node, uvicorn, gunicorn — check with: docker compose top
APP_RESTART_CMD=cd /root/myapp && docker compose up --build -d
APP_LOG_PATH=                    # leave empty if logs are only inside containers
APP_TEST_CMD=                    # leave empty or: cd /root/myapp && docker compose run --rm app pytest
```

---

## 6. Verify the Agent Is Connected

```bash
# On the remote server
systemctl status sentinel-agent
journalctl -u sentinel-agent -f

# From the Brain API
curl -s https://sentinelai.cloud/api/v1/agents/ | jq '.[] | {id, app_name, status, last_seen}'

# Ask Sentinel via Slack or CLI
"show me all connected agents"
"list rmm devices"
```

A connected agent shows `status: online` and a `last_seen` within the last 60 seconds.

---

## 7. Updating the Agent

```bash
# On the remote server
cd /tmp
git clone --depth 1 https://github.com/cocacolasante/Sentinel.git
cp -r Sentinel/sentinel-agent/. /opt/sentinel-agent/
/opt/sentinel-agent/venv/bin/pip install --quiet -r /opt/sentinel-agent/requirements.txt
systemctl restart sentinel-agent
rm -rf Sentinel
```

---

## 8. Uninstalling

```bash
# Stop and disable the service
systemctl stop sentinel-agent
systemctl disable sentinel-agent
rm /etc/systemd/system/sentinel-agent.service
systemctl daemon-reload

# Remove files
rm -rf /opt/sentinel-agent
rm -rf /etc/sentinel-agent

# Remove system user
userdel sentinel-agent

# Revoke the token in the Brain
curl -s -X POST https://sentinelai.cloud/api/v1/agents/YOUR_AGENT_ID/revoke
```

---

## 9. Troubleshooting

### Error: `BASH_SOURCE[0]: unbound variable`

**Cause:** The install script was piped via `curl | bash`. In that mode, bash reads from stdin and `BASH_SOURCE[0]` is unset. This bug existed in older versions of `install.sh`.

**Fix:** Pull the latest `install.sh` — it now downloads files via `git clone` instead of using `BASH_SOURCE`.

```bash
# Re-run with the latest script:
curl -fsSL https://raw.githubusercontent.com/cocacolasante/Sentinel/main/sentinel-agent/install.sh | \
  AGENT_ID="..." AGENT_TOKEN="..." ... bash
```

---

### Error: `/opt/sentinel-agent/venv/bin/pip: No such file or directory`

**Cause:** The agent files were never copied (triggered by the `BASH_SOURCE` bug above), so there was no `requirements.txt` and the venv install failed.

**Fix:** Same as above — use the latest `install.sh`. The new script does a `git clone` before creating the venv, so the files are always present.

---

### Error: `APP_RESTART_CMD` uses `~` or `~/root` — path not found

**Cause:** Tilde (`~`) does not expand inside systemd `EnvironmentFile` entries. `~/root` also incorrectly doubles the home directory (`/root/root` does not exist).

**Fix:** Always use absolute paths in `APP_RESTART_CMD`:

```bash
# WRONG
APP_RESTART_CMD="cd ~/root/myapp && docker compose up -d"
APP_RESTART_CMD="cd ~/myapp && docker compose up -d"

# CORRECT
APP_RESTART_CMD="cd /root/myapp && docker compose up --build -d"
```

---

### Agent connects but shows offline in Brain

```bash
# Check the agent is actually running and sending heartbeats
journalctl -u sentinel-agent -n 50

# Check the Brain can reach the agent's reported IP
curl -s https://sentinelai.cloud/api/v1/agents/YOUR_AGENT_ID/health | jq .

# Confirm BRAIN_URL uses wss:// not ws://
grep BRAIN_URL /etc/sentinel-agent/env
```

---

### `Signature rejected` or `HMAC mismatch` in Brain logs

**Cause:** System clock drift > 60 seconds. The Brain rejects replayed/stale messages.

```bash
# Check and sync time
timedatectl status
timedatectl set-ntp true
chronyc tracking   # if chrony is installed
```

---

### `APP_PROCESS_NAME` not found (process monitor alerts every minute)

The process name must match exactly what `ps aux` shows — not the Docker container name.

```bash
# For a direct Python app:
ps aux | grep python

# For a Docker Compose app, check what runs inside the container:
docker compose top

# Common values:
#   Python apps:  python, python3, uvicorn, gunicorn
#   Node apps:    node, npm
#   Go apps:      the binary name (e.g. myapp, server)
```

Set the correct value in `/etc/sentinel-agent/env` and restart.

---

### Patch dispatch fails or rolls back immediately

```bash
# Check the test command output (if APP_TEST_CMD is set)
journalctl -u sentinel-agent -n 100 | grep -i patch

# Manually verify the restart command works
bash -c "$(grep APP_RESTART_CMD /etc/sentinel-agent/env | cut -d= -f2-)"

# Check git is configured in APP_DIR
cd $(grep APP_DIR /etc/sentinel-agent/env | cut -d= -f2)
git status
```

---

## 10. Security Notes

The `sentinel-agent` system user:
- **Can:** read the app directory (`APP_DIR`), run `APP_RESTART_CMD`, write to `APP_LOG_PATH`
- **Cannot:** read `/etc/sentinel-agent/env` directly (owned `root:sentinel-agent`, mode `640`), access other users' directories, or run arbitrary sudo commands

For production servers, set `SENTINEL_ENV=production`. All patches will require explicit Slack approval before execution.

---

## 11. Architecture Reference

### Message Protocol

Every WebSocket message is JSON:

```json
{
  "type": "HEARTBEAT | LOG_ERROR | REGISTER | PATCH_INSTRUCTION | PATCH_RESULT",
  "agent_id": "uuid",
  "ts": 1741789200,
  "sig": "hmac_sha256_hex",
  "payload": {}
}
```

Messages with timestamp drift > 60 s are rejected (replay protection).

### Brain API Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/agents/provision` | Create agent credentials |
| `GET  /api/v1/agents/` | List all agents |
| `GET  /api/v1/agents/{id}/health` | Health metrics for one agent |
| `GET  /api/v1/agents/{id}/patches` | Patch history + audit log |
| `POST /api/v1/agents/{id}/revoke` | Revoke agent credentials |
| `WS   /ws/agent/{id}` | Agent WebSocket connection |

### Key Environment Variables Summary

| Variable | Example (Docker app) |
|---|---|
| `AGENT_ID` | `e699c2d0-e5d3-476a-aecf-513fe0207f3b` |
| `AGENT_TOKEN` | `0da7a64da243...` |
| `BRAIN_URL` | `wss://sentinelai.cloud/ws/agent` |
| `APP_NAME` | `fluentica-ai` |
| `APP_DIR` | `/root/ailanguagetutor` |
| `APP_PROCESS_NAME` | `python` |
| `APP_HEALTH_URL` | `http://localhost:8000/health` |
| `APP_LOG_PATH` | `/var/log/myapp/app.log` or empty |
| `APP_RESTART_CMD` | `cd /root/ailanguagetutor && docker compose up --build -d` |
| `SENTINEL_ENV` | `production` |
