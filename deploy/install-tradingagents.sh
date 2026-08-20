#!/usr/bin/env bash
# Install/converge /opt/tradingagents and its venv.
#
# This used to clone TauricResearch/TradingAgents, pin a base commit, and apply
# deploy/tradingagents.patch, because our changes to TradingAgents had nowhere to
# live. They now live in 15th-Ave-NE/TradingAgents, so this just tracks that --
# which also fixes a real trap: the pinned base commit plus one patch had drifted
# well behind our work, so a freshly provisioned box would have come up missing
# the A-share vendor, the Gemini model fallback, the progress callback and the
# indicator race fix, while looking like a successful deploy.
set -euo pipefail

APP_DIR="${1:-/opt/ystocker}"
TA_DIR="${2:-/opt/tradingagents}"
RUN_USER="${3:-ystocker}"
TA_REPO="${TA_REPO:-https://github.com/15th-Ave-NE/TradingAgents.git}"
TA_PYTHON="$TA_DIR/venv/bin/python"

if [[ ! -d "$TA_DIR/.git" ]]; then
  git clone "$TA_REPO" "$TA_DIR"
fi

git config --global --add safe.directory "$TA_DIR" 2>/dev/null || true

# Repoint an existing checkout that still tracks the upstream project.
if [[ "$(git -C "$TA_DIR" remote get-url origin 2>/dev/null || true)" != "$TA_REPO" ]]; then
  echo "  repointing origin -> $TA_REPO"
  git -C "$TA_DIR" remote set-url origin "$TA_REPO"
fi

# Same as the app repo: the box matches GitHub, no local state to reconcile.
# `reset --hard` also reattaches HEAD, which the old checkout --detach left off.
git -C "$TA_DIR" fetch origin --prune
git -C "$TA_DIR" reset --hard origin/main
echo "  tradingagents at $(git -C "$TA_DIR" log --oneline -1)"

if [[ ! -x "$TA_PYTHON" ]]; then
  python3.11 -m venv "$TA_DIR/venv"
fi

# Reinstall when the package is missing OR when dependencies moved. `pip install
# -e` is cheap when nothing changed, and skipping it on the strength of a marker
# file is how the box ended up running code that did not match the checkout.
if ! "$TA_PYTHON" -c "import tradingagents" >/dev/null 2>&1 \
   || [[ "$TA_DIR/pyproject.toml" -nt "$TA_DIR/venv/pyvenv.cfg" ]]; then
  "$TA_DIR/venv/bin/pip" install -q --upgrade pip
  "$TA_DIR/venv/bin/pip" install -q --retries 12 --timeout 60 -e "$TA_DIR"
  touch "$TA_DIR/venv/pyvenv.cfg"
fi

chown -R "$RUN_USER:$RUN_USER" "$TA_DIR"

# Import the graph, not just the package: this is what ystocker's child process
# does first, and a missing transitive dependency shows up here rather than as a
# failed analysis twenty minutes later.
"$TA_PYTHON" -c "from tradingagents.graph.trading_graph import TradingAgentsGraph"
echo "  tradingagents import OK"
