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

echo "[$(TS)][4/4] Restarting ystocker service..."
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
REMOTE

log "✓ Deploy complete — https://$HOST"