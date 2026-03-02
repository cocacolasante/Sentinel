#!/usr/bin/env bash
# =============================================================================
# AI Brain — Let's Encrypt SSL Certificate
# Run AFTER DNS is pointing to your server and HTTP is working.
#
# Usage:
#   ./scripts/get_ssl.sh your-domain.com your@email.com
# =============================================================================

set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"

if [[ -z "$DOMAIN" || -z "$EMAIL" ]]; then
    echo "Usage: $0 <domain> <email>"
    echo "Example: $0 brain.example.com admin@example.com"
    exit 1
fi

CERT_DIR="$(dirname "$0")/../nginx/certs"
mkdir -p "$CERT_DIR"

echo "Requesting Let's Encrypt certificate for $DOMAIN..."

# Requires certbot installed on the host
if ! command -v certbot &>/dev/null; then
    sudo apt-get install -y certbot
fi

# Standalone mode — temporarily binds to port 80
# Stop Nginx first so certbot can bind
docker compose stop nginx 2>/dev/null || true

sudo certbot certonly \
    --standalone \
    --non-interactive \
    --agree-tos \
    --email "$EMAIL" \
    -d "$DOMAIN"

# Copy certs into the nginx/certs directory
sudo cp "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" "$CERT_DIR/fullchain.pem"
sudo cp "/etc/letsencrypt/live/$DOMAIN/privkey.pem"   "$CERT_DIR/privkey.pem"
sudo chown "$USER:$USER" "$CERT_DIR/"*.pem

# Update nginx.conf with the real domain
NGINX_CONF="$(dirname "$0")/../nginx/nginx.conf"
sed -i "s/YOUR_DOMAIN/$DOMAIN/g" "$NGINX_CONF"

echo ""
echo "✓ Certificates saved to $CERT_DIR"
echo "✓ nginx.conf updated with domain: $DOMAIN"
echo ""
echo "Restart Nginx to apply:"
echo "  docker compose up -d nginx"
echo ""
echo "Auto-renewal: add to crontab:"
echo "  0 12 * * * certbot renew --quiet && cp /etc/letsencrypt/live/$DOMAIN/*.pem $CERT_DIR/"
