# Sentinel Agent

Lightweight Python daemon that connects remote project servers to Sentinel Brain.
Each agent relays telemetry (heartbeats, logs, process health) over a signed WebSocket
and can receive autonomous code patches from the Brain.

```
┌─────────────────────────────────────────────────────────────────┐
│                       Sentinel Brain                             │
│  ┌──────────┐  ┌──────────────┐  ┌─────────────────────────┐  │
│  │ WS Gateway│  │ agent_tasks  │  │  Skills                  │  │
│  │ /ws/agent │  │ (Celery)     │  │  AgentRegistry           │  │
│  │          │  │ stream→DB    │  │  PatchDispatch           │  │
│  └─────┬────┘  └──────────────┘  └─────────────────────────┘  │
└────────┼────────────────────────────────────────────────────────┘
         │  wss://sentinelai.cloud/ws/agent/{id}
    ─────┼──────────────────────────────────────────────
         │
┌────────▼──────────────────────┐
│     Sentinel Agent (daemon)    │
│  ┌──────────┐  ┌────────────┐ │
│  │  Relay   │  │ Monitors   │ │
│  │ HMAC WS  │  │ heartbeat  │ │
│  │ backoff  │  │ log/http   │ │
│  └──────────┘  │ git/proc   │ │
│  ┌──────────┐  └────────────┘ │
│  │ Patcher  │                 │
│  │ snapshot │                 │
│  │ git apply│                 │
│  └──────────┘                 │
│  Remote Server                │
└───────────────────────────────┘
```

## Quick Install (one-liner)

```bash
curl -fsSL https://raw.githubusercontent.com/cocacolasante/Sentinel/main/sentinel-agent/install.sh | \
  AGENT_ID=<uuid> \
  AGENT_TOKEN=<token> \
  BRAIN_URL=wss://sentinelai.cloud/ws/agent \
  APP_NAME=my-app \
  APP_DIR=/opt/my-app \
  APP_PROCESS_NAME=gunicorn \
  APP_HEALTH_URL=http://localhost:8000/health \
  APP_LOG_PATH=/var/log/my-app/app.log \
  APP_RESTART_CMD="systemctl restart my-app" \
  SENTINEL_ENV=staging \
  bash
```

## Manual Install

```bash
# 1. Provision agent credentials (via Brain CLI or Slack)
brain chat "provision new agent app_name=my-app env=staging"
# → returns AGENT_ID and AGENT_TOKEN

# 2. Clone and install
git clone https://github.com/cocacolasante/Sentinel.git /tmp/sentinel
cd /tmp/sentinel/sentinel-agent

# 3. Run installer with env vars
AGENT_ID=<uuid> AGENT_TOKEN=<token> APP_NAME=my-app bash install.sh
```

## Configuration Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AGENT_ID` | ✅ | — | UUID from Brain provisioning |
| `AGENT_TOKEN` | ✅ | — | HMAC secret from Brain provisioning |
| `BRAIN_URL` | ✅ | `wss://sentinelai.cloud/ws/agent` | Brain WebSocket URL |
| `APP_NAME` | ✅ | `app` | Name of the managed application |
| `APP_DIR` | ✅ | `/opt/app` | Application root directory |
| `APP_PROCESS_NAME` | | `python` | Process name for health checks |
| `APP_HEALTH_URL` | | — | HTTP health endpoint URL |
| `APP_LOG_PATH` | | — | Path to application log file |
| `APP_RESTART_CMD` | | — | Command to restart the application |
| `APP_TEST_CMD` | | — | Command to run test suite |
| `SENTINEL_ENV` | | `staging` | Environment (`staging`/`production`) |
| `HEARTBEAT_INTERVAL` | | `30` | Seconds between heartbeats |
| `RESOURCE_CPU_THRESHOLD` | | `90.0` | CPU % alert threshold |
| `RESOURCE_MEM_THRESHOLD` | | `85.0` | Memory % alert threshold |
| `RESOURCE_DISK_THRESHOLD` | | `90.0` | Disk % alert threshold |

## Verification

```bash
# Check service status
systemctl status sentinel-agent

# Follow logs
journalctl -u sentinel-agent -f

# Verify registration in Brain
brain chat "list mesh agents"

# Check heartbeats in DB
# SELECT * FROM mesh_heartbeats ORDER BY received_at DESC LIMIT 5;
```

## Update

```bash
cd /tmp/sentinel && git pull
cp -r sentinel-agent/. /opt/sentinel-agent/
/opt/sentinel-agent/venv/bin/pip install -r /opt/sentinel-agent/requirements.txt
systemctl restart sentinel-agent
```

## Uninstall

```bash
systemctl stop sentinel-agent
systemctl disable sentinel-agent
rm /etc/systemd/system/sentinel-agent.service
rm -rf /opt/sentinel-agent /etc/sentinel-agent
userdel sentinel-agent
```

## Security

- HMAC-SHA256 signed messages — replay window: 60s
- Agent token stored as SHA-256 hash in Brain DB (never stored in plaintext)
- Production patches require Slack approval before dispatch
- `/etc/sentinel-agent/env` is `chmod 600` — only root readable
- WebSocket connection uses TLS via nginx proxy

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Agent not connecting | Verify `AGENT_ID` and `AGENT_TOKEN`; check firewall allows 443 outbound |
| HMAC failures | Check system clock sync (`timedatectl`); max drift is 60s |
| Process monitor always shows down | Verify `APP_PROCESS_NAME` matches `ps aux` output |
| Log monitor not detecting errors | Verify `APP_LOG_PATH` exists and is readable by sentinel-agent user |
| Patch not applying | Check git is installed; verify `APP_DIR` has a git repo |
