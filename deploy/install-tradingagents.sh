#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-/opt/ystocker}"
TA_DIR="${2:-/opt/tradingagents}"
RUN_USER="${3:-ystocker}"
TA_REPO="https://github.com/TauricResearch/TradingAgents.git"
TA_BASE_REF="a33fd4c0f134485a43553a2c23a63cb14adbd88f"
TA_PATCH_VERSION="1a9533e"
TA_MARKER="$TA_DIR/.ystocker-$TA_PATCH_VERSION"
TA_PYTHON="$TA_DIR/venv/bin/python"

if [[ ! -d "$TA_DIR/.git" ]]; then
  git clone "$TA_REPO" "$TA_DIR"
fi

git config --global --add safe.directory "$TA_DIR" 2>/dev/null || true

if [[ ! -f "$TA_MARKER" ]]; then
  if ! git -C "$TA_DIR" apply --reverse --check "$APP_DIR/deploy/tradingagents.patch" >/dev/null 2>&1; then
    git -C "$TA_DIR" fetch origin "$TA_BASE_REF"
    git -C "$TA_DIR" checkout --detach "$TA_BASE_REF"
    git -C "$TA_DIR" apply --check "$APP_DIR/deploy/tradingagents.patch"
    git -C "$TA_DIR" apply "$APP_DIR/deploy/tradingagents.patch"
  fi

  if [[ ! -x "$TA_PYTHON" ]]; then
    python3.11 -m venv "$TA_DIR/venv"
  fi

  if ! "$TA_PYTHON" -c "import tradingagents" >/dev/null 2>&1; then
    "$TA_DIR/venv/bin/pip" install -q --upgrade pip
    "$TA_DIR/venv/bin/pip" install --retries 12 --timeout 60 -e "$TA_DIR"
  fi

  "$TA_PYTHON" -c "from tradingagents.graph.trading_graph import TradingAgentsGraph"
  touch "$TA_MARKER"
fi

chown -R "$RUN_USER:$RUN_USER" "$TA_DIR"
"$TA_PYTHON" -c "from tradingagents.graph.trading_graph import TradingAgentsGraph"
