#!/usr/bin/env bash
# Sentinel Agent Installer
# Usage: AGENT_ID=... AGENT_TOKEN=... BRAIN_URL=... APP_NAME=... bash install.sh

set -euo pipefail

INSTALL_DIR="/opt/sentinel-agent"
ENV_DIR="/etc/sentinel-agent"
SERVICE_USER="sentinel-agent"
REPO_URL="${SENTINEL_AGENT_REPO:-https://github.com/cocacolasante/sentinel-agent.git}"

echo "==> Installing Sentinel Agent"

# Create system user
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /bin/false "$SERVICE_USER"
    echo "Created system user: $SERVICE_USER"
fi

# Create install directory
mkdir -p "$INSTALL_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# Copy agent files (assume we're running from the repo root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -r "$SCRIPT_DIR"/. "$INSTALL_DIR/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# Create virtualenv
if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# Write environment file
mkdir -p "$ENV_DIR"
chmod 750 "$ENV_DIR"
chown root:"$SERVICE_USER" "$ENV_DIR"

cat > "$ENV_DIR/env" <<EOF
# Sentinel Agent Configuration
AGENT_ID=${AGENT_ID:-}
AGENT_TOKEN=${AGENT_TOKEN:-}
BRAIN_URL=${BRAIN_URL:-wss://sentinelai.cloud/ws/agent}
APP_NAME=${APP_NAME:-app}
APP_DIR=${APP_DIR:-/opt/app}
APP_PROCESS_NAME=${APP_PROCESS_NAME:-python}
APP_HEALTH_URL=${APP_HEALTH_URL:-}
APP_LOG_PATH=${APP_LOG_PATH:-}
APP_RESTART_CMD=${APP_RESTART_CMD:-}
APP_TEST_CMD=${APP_TEST_CMD:-}
SENTINEL_ENV=${SENTINEL_ENV:-staging}
HEARTBEAT_INTERVAL=${HEARTBEAT_INTERVAL:-30}
EOF

chmod 640 "$ENV_DIR/env"
chown root:"$SERVICE_USER" "$ENV_DIR/env"
echo "Wrote config to $ENV_DIR/env"

# Install systemd service
cp "$INSTALL_DIR/sentinel-agent.service" /etc/systemd/system/sentinel-agent.service
systemctl daemon-reload
systemctl enable sentinel-agent
systemctl restart sentinel-agent

echo ""
echo "==> Sentinel Agent installed and started"
echo "    Status: systemctl status sentinel-agent"
echo "    Logs:   journalctl -u sentinel-agent -f"
echo ""
echo "    Agent ID: ${AGENT_ID:-<not set>}"
echo "    Brain URL: ${BRAIN_URL:-wss://sentinelai.cloud/ws/agent}"
