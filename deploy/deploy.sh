#!/usr/bin/env bash
# deploy.sh — force-pull latest code on the EC2 instance and restart all apps
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
# Each app: NAME|PORT|DOMAIN|REQUIREMENTS_FILE|STATIC_DIR
# Add new apps by adding a line to APPS below.
EC2_USER="ec2-user"
HOST="stock.li-family.us"
APP_DIR="/opt/ystocker"
RUN_USER="ystocker"
CERT_EMAIL="admin@li-family.us"

APPS=(
  "ystocker|8000|stock.li-family.us|requirements_stocker.txt|ystocker/static"
  "yplanner|8001|planner.li-family.us|requirements_planner.txt|yplanner/static"
  "yplanter|8002|planter.li-family.us|requirements_planter.txt|yplanter/static"
  "yhome|8003|home.li-family.us|requirements_home.txt|yhome/static"
  "ytracker|8004|tracker.li-family.us|requirements_tracker.txt|ytracker/static"
  "ypay|8005|pay.li-family.us|requirements_pay.txt|ypay/static"
  "yimage|8006|image.li-family.us|requirements_image.txt|yimage/static"
  "ybg|8007|ybackground.li-family.us|requirements_bg.txt|ybg/static"
)

# CloudFormation
CF_STACK_NAME="${CF_STACK_NAME:-ystocker}"   # match your actual stack name (or override via env var)
CF_REGION="us-west-2"
INSTANCE_TYPE="t3.medium"                    # desired EC2 instance type
INSTANCE_ID="i-02c9614bcde54dd59"            # EC2 instance managed by this stack

LOG_PREFIX="[deploy $(date '+%Y-%m-%d %H:%M:%S')]"
log() { echo "$LOG_PREFIX $*"; }

# ── Resolve SSH key / flags ───────────────────────────────────────────────────
SSH_KEY=""
SKIP_CF=false
while getopts "i:s" opt; do
  case $opt in
    i) SSH_KEY="$OPTARG" ;;
    s) SKIP_CF=true ;;      # -s skips the CloudFormation update step
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

# ── Build APPS_CONFIG: semicolon-delimited, for embedding in heredoc ────────
APPS_CONFIG="$(IFS=';'; echo "${APPS[*]}")"

# ── CloudFormation update (idempotent — skips if stack is already up to date) ─
if [[ "$SKIP_CF" == "true" ]]; then
  log "CloudFormation: skipped (-s flag)"
elif [[ -z "$CF_STACK_NAME" ]]; then
  log "CloudFormation: CF_STACK_NAME is empty — skipping"
elif ! command -v aws &>/dev/null; then
  log "CloudFormation: aws CLI not found — skipping"
else
  _CF_STATUS=$(aws cloudformation describe-stacks \
    --stack-name "$CF_STACK_NAME" --region "$CF_REGION" \
    --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "NOT_FOUND")

  if [[ "$_CF_STATUS" == "NOT_FOUND" ]]; then
    log "CloudFormation: stack '$CF_STACK_NAME' not found — skipping"
  else
    _CF_TMPL="$(cd "$(dirname "$0")" && pwd)/cloudformation.yaml"
    log "CloudFormation: checking stack '$CF_STACK_NAME' for changes..."

    if _CF_OUT=$(aws cloudformation deploy \
          --stack-name          "$CF_STACK_NAME" \
          --template-file       "$_CF_TMPL" \
          --region              "$CF_REGION" \
          --capabilities        CAPABILITY_NAMED_IAM \
          --parameter-overrides InstanceType="$INSTANCE_TYPE" \
          --no-fail-on-empty-changeset 2>&1); then
      if echo "$_CF_OUT" | grep -qi "no changes to deploy\|up to date"; then
        log "CloudFormation: no changes — InstanceType already $INSTANCE_TYPE"
      else
        log "CloudFormation: stack updated (InstanceType → $INSTANCE_TYPE) — waiting for EC2..."
        aws ec2 wait instance-status-ok \
          --instance-ids "$INSTANCE_ID" --region "$CF_REGION"
        log "✓ EC2 instance healthy — continuing with code deploy"
      fi
    else
      log "ERROR: CloudFormation update failed:"
      echo "$_CF_OUT" >&2
      exit 1
    fi
  fi
fi

# ── Deploy ───────────────────────────────────────────────────────────────────
log "Connecting to $EC2_USER@$HOST ($APP_DIR)"
log "Apps: $(for a in "${APPS[@]}"; do echo -n "${a%%|*} "; done)"

# NOTE: unquoted heredoc (<<REMOTE not <<'REMOTE') so local shell substitutes
# $APP_DIR, $RUN_USER, $CERT_EMAIL, $APPS_CONFIG before sending to remote.
# Remote shell variables use \$ to survive local expansion.
ssh $SSH_OPTS "$EC2_USER@$HOST" bash <<REMOTE
set -euo pipefail
APP_DIR="$APP_DIR"
RUN_USER="$RUN_USER"
CERT_EMAIL="$CERT_EMAIL"
APPS_RAW="$APPS_CONFIG"
TS() { date '+%Y-%m-%d %H:%M:%S'; }

# Parse apps into arrays
NAMES=(); PORTS=(); DOMAINS=(); REQS=(); STATICS=()
IFS=';' read -ra _APPS <<< "\$APPS_RAW"
for _app in "\${_APPS[@]}"; do
  IFS='|' read -r name port domain req static <<< "\$_app"
  [[ -z "\$name" ]] && continue
  NAMES+=("\$name"); PORTS+=("\$port"); DOMAINS+=("\$domain"); REQS+=("\$req"); STATICS+=("\$static")
done

NUM_APPS=\${#NAMES[@]}
TOTAL_STEPS=\$((3 + NUM_APPS + 2))

# ── Git pull ─────────────────────────────────────────────────────────────────
sudo git config --global --add safe.directory "\$APP_DIR" 2>/dev/null || true

STEP=1
echo "[\$(TS)][\$STEP/\$TOTAL_STEPS] Fetching latest changes from origin..."
sudo git -C "\$APP_DIR" fetch origin 2>&1

STEP=2
echo "[\$(TS)][\$STEP/\$TOTAL_STEPS] Force-resetting to origin/main..."
BEFORE=\$(sudo git -C "\$APP_DIR" rev-parse HEAD)
sudo git -C "\$APP_DIR" reset --hard origin/main 2>&1
AFTER=\$(sudo git -C "\$APP_DIR" rev-parse HEAD)

if [[ "\$BEFORE" == "\$AFTER" ]]; then
  echo "[\$(TS)]    No code changes (already at latest: \${AFTER:0:8})"
else
  echo "[\$(TS)]    Updated: \${BEFORE:0:8} → \${AFTER:0:8}"
  sudo git -C "\$APP_DIR" log --oneline "\${BEFORE}..\${AFTER}" 2>/dev/null | while read line; do
    echo "[\$(TS)]      \$line"
  done
fi

# ── Dependencies ─────────────────────────────────────────────────────────────
STEP=3
echo "[\$(TS)][\$STEP/\$TOTAL_STEPS] Installing/updating dependencies..."
for req in "\${REQS[@]}"; do
  if sudo test -f "\$APP_DIR/\$req"; then
    sudo "\$APP_DIR/venv/bin/pip" install -q -r "\$APP_DIR/\$req" 2>&1
    echo "[\$(TS)]    \$req OK"
  else
    echo "[\$(TS)]    \$req not found — skipping"
  fi
done

# Playwright Chromium browser install — DISABLED.
# Reason: the Azure CDN (playwright.azureedge.net) frequently returns 400
# errors for Mac/Linux ARM builds, breaking the deploy. The Walmart and
# Target scrapers already have multiple API fallback strategies that work
# without a browser, so Playwright is no longer required on EC2.
# To re-enable, set INSTALL_PLAYWRIGHT=1 in this shell before running deploy.
if [[ "\${INSTALL_PLAYWRIGHT:-0}" == "1" ]] \
   && sudo "\$APP_DIR/venv/bin/pip" show playwright >/dev/null 2>&1; then
  echo "[\$(TS)]    Installing Chromium system dependencies via dnf..."
  sudo dnf install -y -q \\
      alsa-lib atk at-spi2-atk at-spi2-core cairo cups-libs dbus-libs \\
      gtk3 libdrm libxkbcommon libXcomposite libXcursor libXdamage \\
      libXext libXfixes libXi libXrandr libXScrnSaver libXtst mesa-libgbm \\
      nspr nss pango xdg-utils 2>&1 | tail -3 || true
  echo "[\$(TS)]    Installing Playwright Chromium browser binary..."
  sudo "\$APP_DIR/venv/bin/playwright" install chromium 2>&1 | tail -3 || \\
      echo "[\$(TS)]    ⚠ Playwright install failed — Walmart scraping uses API fallbacks"
fi

# ── Service setup (function) ─────────────────────────────────────────────────
ensure_service() {
  local name="\$1" port="\$2" step="\$3"

  echo "[\$(TS)][\$step/\$TOTAL_STEPS] Ensuring \$name service..."

  echo "[\$(TS)]    Writing \$name service file..."
  sudo tee "/etc/systemd/system/\${name}.service" > /dev/null <<SERVICEFILE
[Unit]
Description=\${name} Flask app (Gunicorn)
After=network.target

[Service]
User=\${RUN_USER}
Group=\${RUN_USER}
WorkingDirectory=\${APP_DIR}
Environment="PATH=\${APP_DIR}/venv/bin"
ExecStart=\${APP_DIR}/venv/bin/gunicorn \\\\
          --workers 2 \\\\
          --preload \\\\
          --bind 127.0.0.1:\${port} \\\\
          --timeout 120 \\\\
          --access-logfile /var/log/\${name}-access.log \\\\
          --error-logfile  /var/log/\${name}-error.log \\\\
          "\${name}:create_app()"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEFILE
  sudo systemctl daemon-reload
  sudo systemctl enable "\$name"

  sudo touch "/var/log/\${name}-access.log" "/var/log/\${name}-error.log"
  sudo chown "\${RUN_USER}:\${RUN_USER}" "/var/log/\${name}-access.log" "/var/log/\${name}-error.log" 2>/dev/null || true

  echo "[\$(TS)]    Restarting \$name..."
  sudo systemctl restart "\$name"
  sleep 2

  if sudo systemctl is-active --quiet "\$name"; then
    echo "[\$(TS)]    ✓ \$name is running"
    sudo systemctl status "\$name" --no-pager -l | grep -E "Active:|Main PID:" | while read line; do
      echo "[\$(TS)]      \$line"
    done
    # Warm up the app so first user request isn't slow
    echo "[\$(TS)]    Warming up \$name..."
    curl -s -m 30 -o /dev/null -w "[\$(TS)]    Warm-up: HTTP %{http_code} (%{time_total}s)\n" "http://127.0.0.1:\$port/" || true
  else
    echo "[\$(TS)]    ✗ \$name FAILED to start — last 30 log lines:"
    sudo journalctl -u "\$name" -n 30 --no-pager
    exit 1
  fi
}

# ── Deploy each app service ──────────────────────────────────────────────────
for i in \$(seq 0 \$((NUM_APPS - 1))); do
  ensure_service "\${NAMES[\$i]}" "\${PORTS[\$i]}" "\$((4 + i))"
done

# ── Nginx (function) ─────────────────────────────────────────────────────────
NGINX_STEP=\$((4 + NUM_APPS))
echo "[\$(TS)][\$NGINX_STEP/\$TOTAL_STEPS] Ensuring nginx is configured..."
NGINX_CHANGED=false
CERTBOT_DOMAINS=()

ensure_nginx() {
  local name="\$1" port="\$2" domain="\$3" static="\$4"
  local CONF="/etc/nginx/conf.d/\${name}.conf"

  # Skip if config exists, has the right domain, AND passes syntax check
  if sudo test -f "\$CONF" \
     && sudo grep -q "server_name \${domain};" "\$CONF" \
     && sudo nginx -t 2>/dev/null; then
    echo "[\$(TS)]    \$name nginx config up to date"
    return
  fi

  echo "[\$(TS)]    \$name nginx config writing..."
  sudo tee "\$CONF" > /dev/null <<NGINXCONF
server {
    listen 80;
    server_name \${domain};

    location / {
        proxy_pass         http://127.0.0.1:\${port};
        proxy_set_header   Host              \\\$host;
        proxy_set_header   X-Real-IP         \\\$remote_addr;
        proxy_set_header   X-Forwarded-For   \\\$proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }

    location /static/ {
        alias \${APP_DIR}/\${static}/;
        expires 7d;
    }
}
NGINXCONF
  NGINX_CHANGED=true
  CERTBOT_DOMAINS+=("\$domain")
  echo "[\$(TS)]    \$name nginx config updated"
}

for i in \$(seq 0 \$((NUM_APPS - 1))); do
  ensure_nginx "\${NAMES[\$i]}" "\${PORTS[\$i]}" "\${DOMAINS[\$i]}" "\${STATICS[\$i]}"
done

if [[ "\$NGINX_CHANGED" == "true" ]]; then
  sudo rm -f /etc/nginx/conf.d/default.conf /etc/nginx/sites-enabled/default 2>/dev/null || true
  sudo systemctl enable nginx
fi

if sudo nginx -t 2>/dev/null; then
  sudo systemctl restart nginx
  echo "[\$(TS)]    ✓ nginx is running"
else
  echo "[\$(TS)]    ✗ nginx config test failed:"
  sudo nginx -t
  exit 1
fi

# ── SSL (Let's Encrypt) ───────────────────────────────────────────────────────
SSL_STEP=\$((5 + NUM_APPS))
echo "[\$(TS)][\$SSL_STEP/\$TOTAL_STEPS] Ensuring SSL certificates..."

if [[ \${#CERTBOT_DOMAINS[@]} -gt 0 ]]; then
  sudo dnf install -y certbot python3-certbot-nginx -q 2>&1 | tail -1
  for domain in "\${CERTBOT_DOMAINS[@]}"; do
    echo "[\$(TS)]    Certbot: \$domain"
    sudo certbot --nginx --cert-name "\$domain" -d "\$domain" \
      --non-interactive --agree-tos -m "\$CERT_EMAIL" --redirect 2>&1 | tail -3
  done
  echo "[\$(TS)]    ✓ SSL certificates installed"
else
  echo "[\$(TS)]    All nginx configs unchanged — SSL intact"
fi
REMOTE

log "✓ Deploy complete"
for app in "${APPS[@]}"; do
  IFS='|' read -r name port domain req static <<< "$app"
  log "  $name → https://$domain"
done
