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
  "yhome|8003|li-family.us www.li-family.us home.li-family.us|requirements_home.txt|yhome/static"
  "ytracker|8004|tracker.li-family.us|requirements_tracker.txt|ytracker/static"
  "ypay|8005|pay.li-family.us|requirements_pay.txt|ypay/static"
  "yimage|8006|image.li-family.us|requirements_image.txt|yimage/static"
  "ybg|8007|ybackground.li-family.us|requirements_bg.txt|ybg/static"
)

# CloudFormation
CF_STACK_NAME="${CF_STACK_NAME:-ystocker}"   # match your actual stack name (or override via env var)
CF_REGION="us-west-2"
INSTANCE_TYPE="t3.medium"                    # desired EC2 instance type
INSTANCE_ID=""

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

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o User=$EC2_USER"
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
    INSTANCE_ID=$(aws cloudformation describe-stack-resource \
      --stack-name "$CF_STACK_NAME" --logical-resource-id Instance --region "$CF_REGION" \
      --query "StackResourceDetail.PhysicalResourceId" --output text 2>/dev/null || true)
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
        log "CloudFormation: stack updated (InstanceType → $INSTANCE_TYPE) — resolving Instance ID..."
        
        # Resolve current instance ID (in case it was replaced)
        INSTANCE_ID=$(aws cloudformation describe-stack-resource \
          --stack-name "$CF_STACK_NAME" --logical-resource-id Instance --region "$CF_REGION" \
          --query "StackResourceDetail.PhysicalResourceId" --output text 2>/dev/null)
        
        if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
          log "ERROR: could not resolve Instance ID from stack"
          exit 1
        fi

        log "CloudFormation: waiting for EC2 ($INSTANCE_ID) to reach 'running' state..."
        aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$CF_REGION"
        
        log "CloudFormation: waiting for EC2 ($INSTANCE_ID) health checks (may take 2-5 mins)..."
        # We use a loop instead of 'wait instance-status-ok' so the user sees progress
        for i in $(seq 1 30); do
          _ST=$(aws ec2 describe-instance-status --instance-ids "$INSTANCE_ID" --region "$CF_REGION" \
            --query "InstanceStatuses[0].InstanceStatus.Status" --output text 2>/dev/null || echo "initializing")
          if [[ "$_ST" == "ok" ]]; then
            log "✓ EC2 instance healthy ($INSTANCE_ID)"
            break
          fi
          echo "  status: $_ST (attempt $i/30, waiting 15s...)"
          sleep 15
        done
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

log "Waiting for remote bootstrap to finish..."
# Poll for "Bootstrap complete" in /var/log/app-init.log
for i in $(seq 1 60); do
  if ssh $SSH_OPTS "$EC2_USER@$HOST" "grep -q 'Bootstrap complete' /var/log/app-init.log 2>/dev/null" &>/dev/null; then
    log "✓ Remote bootstrap complete"
    break
  fi
  if ssh $SSH_OPTS "$EC2_USER@$HOST" "sudo cloud-init status --long 2>/dev/null | grep -q '^status: error'" &>/dev/null; then
    log "WARNING: Remote bootstrap failed. Continuing with deploy repair..."
    ssh $SSH_OPTS "$EC2_USER@$HOST" "sudo tail -20 /var/log/app-init.log 2>/dev/null" || true
    break
  fi
  if [[ $i -eq 60 ]]; then
    log "WARNING: Remote bootstrap did not finish in time. Continuing anyway..."
  else
    echo "  waiting for bootstrap... (attempt $i/60, 10s sleep)"
    sleep 10
  fi
done

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

echo "[\$(TS)] Ensuring writable runtime directories..."
sudo install -d -o "\$RUN_USER" -g "\$RUN_USER" -m 755 \
  "\$APP_DIR/cache" "\$APP_DIR/cache/agents" "\$APP_DIR/.gunicorn"

# ── Dependencies ─────────────────────────────────────────────────────────────
STEP=3
echo "[\$(TS)][\$STEP/\$TOTAL_STEPS] Installing/updating dependencies..."
for req in "\${REQS[@]}"; do
  if sudo test -f "\$APP_DIR/\$req"; then
    sudo "\$APP_DIR/venv/bin/pip" install -q --retries 12 --timeout 60 -r "\$APP_DIR/\$req" 2>&1
    echo "[\$(TS)]    \$req OK"
  else
    echo "[\$(TS)]    \$req not found — skipping"
  fi
done

# ── Tailwind CSS — rebuild if pytailwindcss is available ─────────────────────
if sudo "\$APP_DIR/venv/bin/pip" show pytailwindcss >/dev/null 2>&1; then
  echo "[\$(TS)] Rebuilding Tailwind CSS..."
  cd "\$APP_DIR"
  sudo "\$APP_DIR/venv/bin/python3" -m pytailwindcss \
    --config tailwind.config.js \
    -i shared/input.css \
    -o shared/tailwind.css \
    --minify 2>&1
  for _app in ystocker yplanner yplanter ytracker ypay yimage ybg; do
    sudo cp "\$APP_DIR/shared/tailwind.css" "\$APP_DIR/\$_app/static/css/tailwind.css" 2>/dev/null || true
  done
  echo "[\$(TS)]    ✓ Tailwind CSS rebuilt"
else
  echo "[\$(TS)]    pytailwindcss not installed — serving committed tailwind.css"
fi

# Playwright Chromium browser install — DISABLED.
# Reason: the Azure CDN (playwright.azureedge.net) frequently returns 400
# errors for Mac/Linux ARM builds, breaking the deploy. The Walmart and
# Target scrapers already have multiple API fallback strategies that work
# without a browser, so Playwright is no longer required on EC2.
# To re-enable, set INSTALL_PLAYWRIGHT=1 in this shell before running deploy.
if [[ "\${INSTALL_PLAYWRIGHT:-0}" == "1" ]]; then
  echo "[\$(TS)]    Installing optional Playwright package..."
  sudo "\$APP_DIR/venv/bin/pip" install -q --retries 12 --timeout 60 'playwright>=1.40.0'
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

# ── CJK font for PDF reports ─────────────────────────────────────────────────
# The agent reports are generated in Chinese and downloaded as PDFs. reportlab
# has to *embed* a font or the file only renders on readers that ship Adobe's
# CJK pack (macOS Preview, Acrobat) and is blank on Windows/Android. It also
# cannot read PostScript/CFF outlines, which rules out google-noto-*-cjk-*
# (sfnt tag OTTO) despite those being the obvious choice -- AR PL UMing is the
# TrueType-outline CJK face in the Amazon Linux repos. ~21 MB.
# See ystocker/report_pdf.py:_register_cjk_font.
if ! rpm -q cjkuni-uming-fonts >/dev/null 2>&1; then
  echo "[\$(TS)]    Installing CJK font for PDF reports..."
  sudo dnf install -y -q cjkuni-uming-fonts 2>&1 | tail -2 || \\
      echo "[\$(TS)]    ⚠ CJK font install failed — Chinese PDFs will not embed a font"
fi

# ── Swap file — OOM cushion ──────────────────────────────────────────────────
# The box has 4 GB and shipped with no swap at all, so any memory spike went
# straight to the OOM killer instead of degrading. 2 GB of swap turns a hard
# kill into a slow request, which is the difference between a 502 and a wait.
if ! sudo swapon --show 2>/dev/null | grep -q /swapfile; then
  echo "[\$(TS)] No swap present — creating 2G swapfile..."
  sudo fallocate -l 2G /swapfile 2>/dev/null \\
    || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab \\
    || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "[\$(TS)]    ✓ swap enabled: \$(free -m | awk '/^Swap:/ {print \$2" MB"}')"
else
  echo "[\$(TS)]    swap already configured — skipping"
fi

# ── Service setup (function) ─────────────────────────────────────────────────
ensure_service() {
  local name="\$1" port="\$2" step="\$3" mem_max="\${4:-400M}"

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
# glibc opens a malloc arena per thread and never shrinks one, so a long-lived
# worker fragments into hundreds of MB of retained heap. Cap the arena count.
Environment="MALLOC_ARENA_MAX=2"
# Keep a runaway app inside its own cgroup. Without this the kernel picks its
# OOM victim globally, so one bloated ystocker worker took unrelated apps down
# with it. These are ceilings, not reservations.
MemoryAccounting=yes
MemoryMax=\${mem_max}
# Kill only gunicorn's master on stop, not everything in the cgroup.
#
# ystocker launches the TradingAgents analysis as a detached subprocess that can
# run for tens of minutes; start_new_session gives it its own session, but a
# session is not a cgroup, so the default KillMode=control-group made every
# \`systemctl restart\` kill in-flight runs -- a deploy silently destroyed a
# ten-minute analysis and the ~22 Gemini Pro calls it had already paid for.
# gunicorn's master reaps its own workers on SIGTERM, so nothing is orphaned.
KillMode=process
ExecStart=\${APP_DIR}/venv/bin/gunicorn \\\\
          --workers 2 \\\\
          --preload \\\\
          --bind 127.0.0.1:\${port} \\\\
          --timeout 120 \\\\
          --max-requests 200 \\\\
          --max-requests-jitter 50 \\\\
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
  # ystocker carries pandas/matplotlib/prophet plus the 500-ticker caches and
  # legitimately needs ~1 GB; the other seven sit around 100 MB each.
  if [[ "\${NAMES[\$i]}" == "ystocker" ]]; then _mem="1800M"; else _mem="400M"; fi
  ensure_service "\${NAMES[\$i]}" "\${PORTS[\$i]}" "\$((4 + i))" "\$_mem"
done

# ── Nginx (function) ─────────────────────────────────────────────────────────
NGINX_STEP=\$((4 + NUM_APPS))
echo "[\$(TS)][\$NGINX_STEP/\$TOTAL_STEPS] Ensuring nginx is configured..."
NGINX_CHANGED=false
CERTBOT_DOMAINS=()

ensure_nginx() {
  local name="\$1" port="\$2" domain="\$3" static="\$4"
  local CONF="/etc/nginx/conf.d/\${name}.conf"

  # Skip if config exists, has the right domains AND the right proxy port
  if sudo test -f "\$CONF" \
     && sudo grep -q "server_name \${domain};" "\$CONF" \
     && sudo grep -q "proxy_pass http://127.0.0.1:\${port};" "\$CONF" \
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
        proxy_set_header   X-Forwarded-Proto \\\$scheme;
        proxy_set_header   X-Forwarded-Host  \\\$host;
        proxy_read_timeout 130s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 130s;
        # Avoid buffering large responses to disk
        proxy_buffer_size       16k;
        proxy_buffers           8 64k;
        proxy_busy_buffers_size 128k;
        # Retry on upstream failure so a crashing worker doesn't cause 502
        proxy_next_upstream     error timeout http_502 http_503;
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
  SSL_FAILED=()
  for domains in "\${CERTBOT_DOMAINS[@]}"; do
    echo "[\$(TS)]    Certbot: \$domains"
    CERT_D_FLAGS=""
    for d in \$domains; do
      CERT_D_FLAGS="\$CERT_D_FLAGS -d \$d"
    done
    FIRST_D=\$(echo \$domains | awk '{print \$1}')
    # Isolate each domain: this script runs under 'set -euo pipefail', so without
    # the explicit '|| true' one unresolvable domain aborts the entire deploy and
    # every app after it in the list silently loses its certificate.
    CERT_RC=0
    sudo certbot --nginx --cert-name "\$FIRST_D" \$CERT_D_FLAGS \
      --non-interactive --agree-tos -m "$CERT_EMAIL" --redirect \
      --allow-subset-of-names > /tmp/certbot-\$FIRST_D.log 2>&1 || CERT_RC=\$?
    tail -3 /tmp/certbot-\$FIRST_D.log
    if [[ \$CERT_RC -ne 0 ]]; then
      echo "[\$(TS)]    ✗ Certbot FAILED for \$domains (rc=\$CERT_RC) — continuing"
      SSL_FAILED+=("\$domains")
    fi
  done
  if [[ \${#SSL_FAILED[@]} -gt 0 ]]; then
    echo "[\$(TS)]    ⚠ SSL incomplete — no certificate for: \${SSL_FAILED[*]}"
    echo "[\$(TS)]      These hosts will serve a MISMATCHED cert until fixed."
  else
    echo "[\$(TS)]    ✓ SSL certificates installed"
  fi
else
  echo "[\$(TS)]    All nginx configs unchanged — SSL intact"
fi
REMOTE

log "✓ Deploy complete"
for app in "${APPS[@]}"; do
  IFS='|' read -r name port domain req static <<< "$app"
  log "  $name → https://$domain"
done
