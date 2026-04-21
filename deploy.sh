#!/usr/bin/env bash
# deploy.sh — force-pull latest code on the EC2 instance and restart all apps
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
# Each app: NAME  PORT  DOMAIN  REQUIREMENTS_FILE  STATIC_DIR
# Add new apps by adding a line to APPS below.
EC2_USER="ec2-user"
HOST="stock.li-family.us"
APP_DIR="/opt/ystocker"
RUN_USER="ystocker"
CERT_EMAIL="admin@li-family.us"

APPS=(
  "ystocker|8000|stock.li-family.us|requirements.txt|ystocker/static"
  "yplanner|8001|planner.li-family.us|requirements_planner.txt|yplanner/static"
)

LOG_PREFIX="[deploy $(date '+%Y-%m-%d %H:%M:%S')]"
log() { echo "$LOG_PREFIX $*"; }

# ── Resolve SSH key ──────────────────────────────────────────────────────────
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

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o User=$EC2_USER"
if [[ -n "$SSH_KEY" ]]; then
  SSH_OPTS="$SSH_OPTS -i $SSH_KEY"
fi

# ── Build the APPS config string to send to remote ──────────────────────────
# Join with semicolon delimiter (safe since no field contains ;)
APPS_CONFIG="$(IFS=';'; echo "${APPS[*]}")"

# ── Deploy ───────────────────────────────────────────────────────────────────
log "Connecting to $EC2_USER@$HOST ($APP_DIR)"
log "Apps: $(for a in "${APPS[@]}"; do echo -n "${a%%|*} "; done)"

ssh $SSH_OPTS "$EC2_USER@$HOST" bash -s -- \
  "$APP_DIR" "$RUN_USER" "$CERT_EMAIL" "$APPS_CONFIG" <<'REMOTE'
set -euo pipefail
APP_DIR="$1"
RUN_USER="$2"
CERT_EMAIL="$3"
APPS_RAW="$4"
TS() { date '+%Y-%m-%d %H:%M:%S'; }

# Parse apps into arrays
NAMES=(); PORTS=(); DOMAINS=(); REQS=(); STATICS=()
IFS=';' read -ra _APPS <<< "$APPS_RAW"
for _app in "${_APPS[@]}"; do
  IFS='|' read -r name port domain req static <<< "$_app"
  [[ -z "$name" ]] && continue
  NAMES+=("$name"); PORTS+=("$port"); DOMAINS+=("$domain"); REQS+=("$req"); STATICS+=("$static")
done

NUM_APPS=${#NAMES[@]}
TOTAL_STEPS=$((3 + NUM_APPS + 2))  # git + deps + N services + nginx + ssl

# ── Git pull ─────────────────────────────────────────────────────────────────
sudo git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

STEP=1
echo "[$(TS)][$STEP/$TOTAL_STEPS] Fetching latest changes from origin..."
sudo git -C "$APP_DIR" fetch origin 2>&1

STEP=2
echo "[$(TS)][$STEP/$TOTAL_STEPS] Force-resetting to origin/main..."
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

# ── Dependencies ─────────────────────────────────────────────────────────────
STEP=3
echo "[$(TS)][$STEP/$TOTAL_STEPS] Installing/updating dependencies..."
for req in "${REQS[@]}"; do
  if sudo test -f "$APP_DIR/$req"; then
    sudo "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/$req" 2>&1
    echo "[$(TS)]    $req OK"
  else
    echo "[$(TS)]    $req not found — skipping"
  fi
done

# ── Service setup (function) ─────────────────────────────────────────────────
ensure_service() {
  local name="$1" port="$2" step="$3"

  echo "[$(TS)][$step/$TOTAL_STEPS] Ensuring $name service..."

  if ! sudo test -f "/etc/systemd/system/${name}.service"; then
    echo "[$(TS)]    Service file missing — installing..."
    sudo tee "/etc/systemd/system/${name}.service" > /dev/null <<SERVICEFILE
[Unit]
Description=${name} Flask app (Gunicorn)
After=network.target

[Service]
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${APP_DIR}
Environment="PATH=${APP_DIR}/venv/bin"
ExecStart=${APP_DIR}/venv/bin/gunicorn \\
          --workers 1 \\
          --bind 127.0.0.1:${port} \\
          --timeout 120 \\
          --access-logfile /var/log/${name}-access.log \\
          --error-logfile  /var/log/${name}-error.log \\
          "${name}:create_app()"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEFILE
    sudo systemctl daemon-reload
    sudo systemctl enable "$name"
    echo "[$(TS)]    Service file installed and enabled"
  fi

  sudo touch "/var/log/${name}-access.log" "/var/log/${name}-error.log"
  sudo chown "${RUN_USER}:${RUN_USER}" "/var/log/${name}-access.log" "/var/log/${name}-error.log" 2>/dev/null || true

  echo "[$(TS)]    Restarting $name..."
  sudo systemctl restart "$name"
  sleep 2

  if sudo systemctl is-active --quiet "$name"; then
    echo "[$(TS)]    ✓ $name is running"
    sudo systemctl status "$name" --no-pager -l | grep -E "Active:|Main PID:" | while read line; do
      echo "[$(TS)]      $line"
    done
  else
    echo "[$(TS)]    ✗ $name FAILED to start — last 30 log lines:"
    sudo journalctl -u "$name" -n 30 --no-pager
    exit 1
  fi
}

# ── Deploy each app service ──────────────────────────────────────────────────
for i in $(seq 0 $((NUM_APPS - 1))); do
  ensure_service "${NAMES[$i]}" "${PORTS[$i]}" "$((4 + i))"
done

# ── Nginx (function) ─────────────────────────────────────────────────────────
NGINX_STEP=$((4 + NUM_APPS))
echo "[$(TS)][$NGINX_STEP/$TOTAL_STEPS] Ensuring nginx is configured..."
NGINX_CHANGED=false

ensure_nginx() {
  local name="$1" port="$2" domain="$3" static="$4"

  if ! sudo test -f "/etc/nginx/conf.d/${name}.conf"; then
    echo "[$(TS)]    $name nginx config missing — installing..."
    sudo tee "/etc/nginx/conf.d/${name}.conf" > /dev/null <<NGINXCONF
server {
    listen 80;
    server_name ${domain};

    location / {
        proxy_pass         http://127.0.0.1:${port};
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }

    location /static/ {
        alias ${APP_DIR}/${static}/;
        expires 7d;
    }
}
NGINXCONF
    NGINX_CHANGED=true
    echo "[$(TS)]    $name nginx config installed"
  fi
}

for i in $(seq 0 $((NUM_APPS - 1))); do
  ensure_nginx "${NAMES[$i]}" "${PORTS[$i]}" "${DOMAINS[$i]}" "${STATICS[$i]}"
done

if [[ "$NGINX_CHANGED" == "true" ]]; then
  sudo rm -f /etc/nginx/conf.d/default.conf /etc/nginx/sites-enabled/default 2>/dev/null || true
  sudo systemctl enable nginx
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
SSL_STEP=$((5 + NUM_APPS))
echo "[$(TS)][$SSL_STEP/$TOTAL_STEPS] Checking SSL certificates..."
CERTBOT_DOMAINS=""

for domain in "${DOMAINS[@]}"; do
  if ! sudo test -f "/etc/letsencrypt/live/${domain}/fullchain.pem"; then
    CERTBOT_DOMAINS="$CERTBOT_DOMAINS -d $domain"
  fi
done

if [[ -n "$CERTBOT_DOMAINS" ]]; then
  echo "[$(TS)]    Installing SSL certificate(s) via Let's Encrypt..."
  sudo dnf install -y certbot python3-certbot-nginx -q 2>&1 | tail -1
  sudo certbot --nginx $CERTBOT_DOMAINS --non-interactive --agree-tos -m "$CERT_EMAIL" --redirect
  echo "[$(TS)]    ✓ SSL certificate(s) installed"
else
  echo "[$(TS)]    SSL certificates already present — skipping"
fi
REMOTE

log "✓ Deploy complete"
for app in "${APPS[@]}"; do
  IFS='|' read -r name port domain req static <<< "$app"
  log "  $name → https://$domain"
done
