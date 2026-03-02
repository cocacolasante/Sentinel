#!/usr/bin/env bash
# =============================================================================
# AI Brain — Ubuntu Server Bootstrap
# Run this ONCE on a fresh Ubuntu 22.04 server as a sudo user (not root).
#
# What it does:
#   1. Updates the system
#   2. Installs Docker + Docker Compose
#   3. Configures UFW firewall (80, 443, 22 only)
#   4. Creates ~/ai-brain project directory
#   5. Optionally installs Fail2ban
#
# Usage:
#   chmod +x server_setup.sh && ./server_setup.sh
# =============================================================================

set -euo pipefail

BRAIN_DIR="$HOME/ai-brain"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  AI Brain — Server Bootstrap"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 1. System update ──────────────────────────────────────────
echo "[1/5] Updating system packages..."
sudo apt-get update -qq
sudo apt-get upgrade -y -qq
sudo apt-get install -y -qq curl git ufw fail2ban

# ── 2. Docker ─────────────────────────────────────────────────
echo "[2/5] Installing Docker..."
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    echo "  ✓ Docker installed. NOTE: log out and back in for group membership."
else
    echo "  Docker already installed — skipping."
fi

# Docker Compose (v2 plugin)
if ! docker compose version &>/dev/null; then
    sudo apt-get install -y -qq docker-compose-plugin
fi
echo "  ✓ Docker Compose: $(docker compose version --short 2>/dev/null || echo 'installed')"

# ── 3. Firewall ───────────────────────────────────────────────
echo "[3/5] Configuring UFW firewall..."
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp    comment 'SSH'
sudo ufw allow 80/tcp    comment 'HTTP'
sudo ufw allow 443/tcp   comment 'HTTPS'
sudo ufw --force enable
echo "  ✓ UFW active. Open ports: 22, 80, 443"

# ── 4. Fail2ban ───────────────────────────────────────────────
echo "[4/5] Enabling Fail2ban..."
sudo systemctl enable fail2ban
sudo systemctl start  fail2ban
echo "  ✓ Fail2ban running"

# ── 5. Project directory ──────────────────────────────────────
echo "[5/5] Creating project directory: $BRAIN_DIR"
mkdir -p "$BRAIN_DIR"/{app/{brain,memory,router,integrations},nginx/certs,scripts}
echo "  ✓ Directory tree created"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Bootstrap complete!"
echo ""
echo "  Next steps:"
echo "  1. Copy your project files to $BRAIN_DIR"
echo "     (scp -r . user@server:~/ai-brain/)"
echo "  2. cd ~/ai-brain && cp .env.example .env"
echo "  3. Fill in all values in .env"
echo "  4. Run: ./scripts/deploy.sh"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
