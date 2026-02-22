#!/usr/bin/env bash
# deploy.sh — pull latest code on the EC2 instance and restart ystocker
set -euo pipefail

HOST="stock.li-family.us"
EC2_USER="ec2-user"
APP_DIR="/opt/ystocker"

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
      break
    fi
  done
fi

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"
if [[ -n "$SSH_KEY" ]]; then
  SSH_OPTS="$SSH_OPTS -i $SSH_KEY"
fi

# ── Deploy ───────────────────────────────────────────────────────────────────
echo "→ Deploying to $EC2_USER@$HOST ($APP_DIR)"

ssh $SSH_OPTS "$EC2_USER@$HOST" bash <<'REMOTE'
set -euo pipefail
APP_DIR="/opt/ystocker"

echo "[1/3] Pulling latest code..."
sudo git -C "$APP_DIR" pull origin main

echo "[2/3] Installing dependencies..."
sudo "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "[3/3] Restarting service..."
sudo systemctl restart ystocker
sudo systemctl is-active --quiet ystocker && echo "✓ ystocker is running" || { echo "✗ ystocker failed to start"; sudo journalctl -u ystocker -n 20 --no-pager; exit 1; }
REMOTE

echo "✓ Deploy complete — https://$HOST"
