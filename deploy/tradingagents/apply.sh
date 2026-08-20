#!/usr/bin/env bash
#
# Apply the local TradingAgents patch series to /opt/tradingagents.
#
# Why this exists
# ---------------
# /opt/tradingagents is a checkout of TauricResearch/TradingAgents, which we do
# not have write access to. Every fix and feature we have added therefore lives
# only as local commits — on a laptop and on the box. The box has already been
# replaced once, which would have taken all of it with it, so the series is kept
# here, in a repo we do own and push.
#
# The patches are applied with `git am`, not `git apply`, on purpose: they land as
# real commits, so a later `git pull` of upstream conflicts loudly instead of
# silently reverting them.
#
# Usage (on the box):
#   sudo bash /opt/ystocker/deploy/tradingagents/apply.sh
#
# Idempotent: a patch whose changes are already present is skipped, so re-running
# after a partial failure is safe.
set -uo pipefail

TA_DIR="${TRADINGAGENTS_DIR:-/opt/tradingagents}"
SERIES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -d "$TA_DIR/.git" ]]; then
  echo "!! $TA_DIR is not a git checkout — clone TradingAgents there first." >&2
  exit 1
fi

cd "$TA_DIR"

# Abort a half-finished am from an earlier run, or `git am` refuses to start.
if [[ -d .git/rebase-apply ]]; then
  echo "-- clearing an interrupted git am"
  git am --abort 2>/dev/null || true
fi

applied=0 skipped=0 failed=0

for patch in "$SERIES_DIR"/0*.patch; do
  name="$(basename "$patch")"

  # Already applied? Test whether the patch reverses cleanly, which is true only
  # when its changes are already in the tree.
  #
  # Not by comparing subject lines: `git format-patch` wraps a long subject onto
  # a continuation line and MIME-encodes a non-ASCII one (=?UTF-8?q?...?=), so
  # the extracted text matched neither the log nor itself. That version reported
  # every already-applied patch as a CONFLICT and exited 1 — alarming, and wrong.
  # Nor by SHA: re-applying on a different base produces a different one.
  if git apply --reverse --check "$patch" 2>/dev/null; then
    echo "== skip    $name (already applied)"
    skipped=$((skipped + 1))
    continue
  fi

  if ! git apply --check "$patch" 2>/dev/null; then
    echo "!! CONFLICT $name"
    echo "           does not apply to $(git log --oneline -1)"
    failed=$((failed + 1))
    continue
  fi

  if git -c user.email=ops@li-family.us -c user.name=ystocker-ops am "$patch" >/dev/null 2>&1; then
    echo "== applied $name"
    applied=$((applied + 1))
  else
    echo "!! FAILED  $name (am aborted)"
    git am --abort 2>/dev/null || true
    failed=$((failed + 1))
  fi
done

echo
echo "-- applied=$applied skipped=$skipped failed=$failed"
echo "-- $TA_DIR now at: $(git log --oneline -1)"

# The A-share vendor needs nothing extra, but the CJK PDF font and the Gemini
# httpx floor are easy to forget on a fresh box; check rather than assume.
python_bin="${TRADINGAGENTS_PYTHON:-$TA_DIR/venv/bin/python}"
if [[ -x "$python_bin" ]]; then
  "$python_bin" - <<'PY' || true
import importlib.util as u
for mod, why in (("pandas", "A-share OHLCV parsing"),
                 ("requests", "东财/新浪 HTTP"),
                 ("stockstats", "indicator computation")):
    print(f"   {mod:11} {'ok' if u.find_spec(mod) else 'MISSING — ' + why}")
PY
fi

[[ $failed -eq 0 ]] || exit 1
