#!/usr/bin/env bash
#
# Deploy li-family.us: push nothing, just make the box match the repos.
#
#   bash deploy/ship.sh              # ystocker + TradingAgents, then restart
#   bash deploy/ship.sh --ystocker   # ystocker only (skip TradingAgents)
#   bash deploy/ship.sh --check      # report what is deployed, change nothing
#
# Both checkouts are plain `fetch` + `reset --hard`, which is why this is short.
# TradingAgents used to need a patch series applied by hand, because /opt was a
# checkout of TauricResearch/TradingAgents and our commits had nowhere to live.
# They now live in 15th-Ave-NE/TradingAgents, so the box tracks that instead and
# the patch flow is gone.
#
# Restarting is safe while an analysis is running: the unit sets
# KillMode=process, so systemd signals only gunicorn's master and the detached
# TradingAgents child survives the deploy. Before that, deploying killed runs
# mid-debate and threw away the API spend.
set -uo pipefail

INSTANCE="${YSTOCKER_INSTANCE:-i-059a024daff6bd015}"
REGION="${AWS_REGION:-us-west-2}"
TA_REMOTE="https://github.com/15th-Ave-NE/TradingAgents.git"

WITH_TA=1
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --ystocker) WITH_TA=0 ;;
    --check)    CHECK_ONLY=1 ;;
    -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

run_remote() {
  # Send one shell script to the box and wait for it, rather than polling a
  # command id by hand.
  local script="$1"
  local b64 cmd status
  b64="$(printf '%s' "$script" | base64 | tr -d '\n')"
  cmd="$(aws ssm send-command \
        --instance-ids "$INSTANCE" --region "$REGION" \
        --document-name AWS-RunShellScript \
        --parameters "{\"commands\":[\"echo -n '$b64' | base64 -d > /tmp/ship_step.sh\",\"bash /tmp/ship_step.sh\"]}" \
        --query 'Command.CommandId' --output text)" || return 1
  for _ in $(seq 1 60); do
    sleep 5
    status="$(aws ssm get-command-invocation --command-id "$cmd" \
              --instance-id "$INSTANCE" --region "$REGION" \
              --query 'Status' --output text 2>/dev/null)" || continue
    [[ "$status" == "InProgress" || "$status" == "Pending" || "$status" == "Delayed" ]] && continue
    break
  done
  aws ssm get-command-invocation --command-id "$cmd" \
    --instance-id "$INSTANCE" --region "$REGION" \
    --query 'StandardOutputContent' --output text
  local err
  err="$(aws ssm get-command-invocation --command-id "$cmd" --instance-id "$INSTANCE" \
        --region "$REGION" --query 'StandardErrorContent' --output text 2>/dev/null)"
  [[ -n "$err" && "$err" != "None" ]] && printf '%s\n' "--- stderr ---" "$err"
  [[ "$status" == "Success" ]]
}

if [[ $CHECK_ONLY -eq 1 ]]; then
  run_remote '
set -u
echo "ystocker      : $(cd /opt/ystocker && sudo git log --oneline -1)"
echo "tradingagents : $(cd /opt/tradingagents && sudo git log --oneline -1)"
echo "ta remote     : $(cd /opt/tradingagents && sudo git remote get-url origin)"
echo "service       : $(systemctl is-active ystocker)"
echo "kill mode     : $(systemctl show ystocker -p KillMode --value)"
printf "http          : agents=%s multiples=%s\n" \
  "$(curl -s -o /dev/null -w "%{http_code}" https://stock.li-family.us/agents)" \
  "$(curl -s -o /dev/null -w "%{http_code}" https://stock.li-family.us/multiples)"
'
  exit $?
fi

# Warn about unpushed work before deploying: `reset --hard` on the box takes what
# GitHub has, so a commit still sitting on the laptop silently will not ship.
for repo in "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" "$HOME/workspace/TradingAgents"; do
  [[ -d "$repo/.git" ]] || continue
  name="$(basename "$repo")"
  if [[ -n "$(git -C "$repo" status --porcelain 2>/dev/null)" ]]; then
    echo "!! $name has uncommitted changes — they will NOT be deployed"
  fi
  # TradingAgents' own fork is the deploy source; its remote may be named
  # anything locally, so compare against the URL rather than a remote name.
  target="$(git -C "$repo" remote -v 2>/dev/null | awk -v u="$TA_REMOTE" '$2==u {print $1; exit}')"
  [[ "$name" == "TradingAgents" ]] || target=origin
  if [[ -n "$target" ]]; then
    ahead="$(git -C "$repo" rev-list --count "$target/main..HEAD" 2>/dev/null || echo 0)"
    [[ "$ahead" != "0" ]] && echo "!! $name is $ahead commit(s) ahead of $target/main — push first"
  fi
done

STEPS='
set -u
echo "== ystocker"
cd /opt/ystocker && sudo git fetch origin --prune -q && sudo git reset --hard -q origin/main
sudo git log --oneline -1
'

if [[ $WITH_TA -eq 1 ]]; then
STEPS="$STEPS"'
echo "== tradingagents"
cd /opt/tradingagents
# Repoint if the box is still tracking the upstream project rather than our fork.
if [[ "$(sudo git remote get-url origin)" != "'"$TA_REMOTE"'" ]]; then
  echo "   repointing origin -> '"$TA_REMOTE"'"
  sudo git remote set-url origin "'"$TA_REMOTE"'"
fi
sudo git fetch origin --prune -q && sudo git reset --hard -q origin/main
sudo git log --oneline -1
'
fi

STEPS="$STEPS"'
echo "== restart"
sudo systemctl restart ystocker
sleep 10
echo "   service: $(systemctl is-active ystocker)"
printf "   http   : agents=%s multiples=%s\n" \
  "$(curl -s -o /dev/null -w "%{http_code}" https://stock.li-family.us/agents)" \
  "$(curl -s -o /dev/null -w "%{http_code}" https://stock.li-family.us/multiples)"
'

run_remote "$STEPS"
