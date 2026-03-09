#!/usr/bin/env bash
# deploy.sh — force-pull latest code on the EC2 instance and restart ystocker
set -euo pipefail

HOST="stock.li-family.us"
EC2_USER="ec2-user"
APP_DIR="/opt/ystocker"

LOG_PREFIX="[deploy $(date '+%Y-%m-%d %H:%M:%S')]"
log() { echo "$LOG_PREFIX $*"; }

# ── Resolve SSH key ──────────────────────────────────────────────────────────
# Accept an explicit key via -i flag, otherwise auto-detect from ~/.ssh
SSH_KEY=""
while getopts "i:" opt; do
  case $opt in
    i) SSH_KEY="$OPTARG" ;;
  esac
done

if [[ -z "$SSH_KEY" ]]; then
  for candidate in ~/.ssh/*.pem ~/.ssh/id_rsa ~/.ssh/id_ed25519; do
    if [[ -f "$candidate" ]]; then
      SSH_KEY="$candidate"
      log "Auto-detected SSH key: $candidate"
      break
    fi
  done
fi

[[ -z "$SSH_KEY" ]] && log "WARNING: no SSH key found — relying on ssh-agent or default config"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"
if [[ -n "$SSH_KEY" ]]; then
  SSH_OPTS="$SSH_OPTS -i $SSH_KEY"
fi

# ── Deploy ───────────────────────────────────────────────────────────────────
log "Connecting to $EC2_USER@$HOST ($APP_DIR)"

ssh $SSH_OPTS "$EC2_USER@$HOST" bash <<'REMOTE'
set -euo pipefail
APP_DIR="/opt/ystocker"
TS() { date '+%Y-%m-%d %H:%M:%S'; }

sudo git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

echo "[$(TS)][1/4] Fetching latest changes from origin..."
sudo git -C "$APP_DIR" fetch origin 2>&1

echo "[$(TS)][2/4] Force-resetting to origin/main..."
BEFORE=$(sudo git -C "$APP_DIR" rev-parse HEAD)
sudo git -C "$APP_DIR" reset --hard origin/main 2>&1
AFTER=$(sudo git -C "$APP_DIR" rev-parse HEAD)

if [[ "$BEFORE" == "$AFTER" ]]; then
  echo "[$(TS)]    No code changes (already at latest: ${AFTER:0:8})"
else
  echo "[$(TS)]    Updated: ${BEFORE:0:8} → ${AFTER:0:8}"
  sudo git -C "$APP_DIR" log --oneline "${BEFORE}..${AFTER}" 2>/dev/null | while read line; do
    echo "[$(TS)]      $line"
  done
fi

echo "[$(TS)][3/4] Installing/updating dependencies..."
sudo "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" 2>&1
echo "[$(TS)]    Dependencies OK"

echo "[$(TS)][4/5] Ensuring ystocker service is installed..."
if ! sudo systemctl list-unit-files ystocker.service &>/dev/null || \
   ! sudo test -f /etc/systemd/system/ystocker.service; then
  echo "[$(TS)]    Service file missing — installing..."
  sudo tee /etc/systemd/system/ystocker.service > /dev/null << 'SERVICE'
[Unit]
Description=yStocker Flask app (Gunicorn)
After=network.target

[Service]
User=ystocker
Group=ystocker
WorkingDirectory=/opt/ystocker
Environment="PATH=/opt/ystocker/venv/bin"
ExecStart=/opt/ystocker/venv/bin/gunicorn \
          --workers 1 \
          --bind 127.0.0.1:8000 \
          --timeout 120 \
          --access-logfile /var/log/ystocker-access.log \
          --error-logfile  /var/log/ystocker-error.log \
          "ystocker:create_app()"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE
  sudo systemctl daemon-reload
  sudo systemctl enable ystocker
  echo "[$(TS)]    Service file installed and enabled"
fi

# Ensure log files exist and are owned by the app user
sudo touch /var/log/ystocker-access.log /var/log/ystocker-error.log
sudo chown ystocker:ystocker /var/log/ystocker-access.log /var/log/ystocker-error.log 2>/dev/null || true

echo "[$(TS)]    Restarting ystocker service..."
sudo systemctl restart ystocker
sleep 2

if sudo systemctl is-active --quiet ystocker; then
  echo "[$(TS)]    ✓ ystocker is running"
  sudo systemctl status ystocker --no-pager -l | grep -E "Active:|Main PID:|Loaded:" | while read line; do
    echo "[$(TS)]      $line"
  done
else
  echo "[$(TS)]    ✗ ystocker FAILED to start — last 30 log lines:"
  sudo journalctl -u ystocker -n 30 --no-pager
  exit 1
fi

# ── nginx ─────────────────────────────────────────────────────────────────────
echo "[$(TS)][5/6] Ensuring nginx is configured..."
if ! sudo test -f /etc/nginx/conf.d/ystocker.conf; then
  echo "[$(TS)]    nginx config missing — installing..."
  sudo tee /etc/nginx/conf.d/ystocker.conf > /dev/null << 'NGINX'
server {
    listen 80;
    server_name stock.li-family.us;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }

    location /static/ {
        alias /opt/ystocker/ystocker/static/;
        expires 7d;
    }
}
NGINX
  sudo rm -f /etc/nginx/conf.d/default.conf /etc/nginx/sites-enabled/default 2>/dev/null || true
  sudo systemctl enable nginx
  echo "[$(TS)]    nginx config installed"
fi

if sudo nginx -t 2>/dev/null; then
  sudo systemctl restart nginx
  echo "[$(TS)]    ✓ nginx is running"
else
  echo "[$(TS)]    ✗ nginx config test failed:"
  sudo nginx -t
  exit 1
fi

# ── SSL (Let's Encrypt) ───────────────────────────────────────────────────────
if ! sudo test -f /etc/letsencrypt/live/stock.li-family.us/fullchain.pem; then
  echo "[$(TS)][6/6] Installing SSL certificate via Let's Encrypt..."
  sudo dnf install -y certbot python3-certbot-nginx -q 2>&1 | tail -1
  sudo certbot --nginx -d stock.li-family.us --non-interactive --agree-tos -m admin@li-family.us --redirect
  echo "[$(TS)]    ✓ SSL certificate installed"
else
  echo "[$(TS)][6/6] SSL certificate already present — skipping"
fi
REMOTE

log "✓ Deploy complete — https://$HOST"