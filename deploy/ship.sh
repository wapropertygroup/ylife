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

# Warn about work that will not ship. `reset --hard` on the box takes whatever
# GitHub has, so an uncommitted edit or an unpushed commit looks deployed and
# is not -- a nasty way to spend twenty minutes debugging prod.
#
# The check asks the remote for its main SHA rather than comparing against a
# local `<remote>/main` ref, because that ref may not exist: the TradingAgents
# clone calls the fork `upstream` and has never fetched it, so `upstream/main`
# is an unknown revision. Comparing against a missing ref fails, and a failed
# comparison that defaults to "0 commits ahead" would report everything pushed
# when nothing is -- silence exactly where the warning is needed.
preflight() {
  local repo="$1" name="$2" url="$3"
  [[ -d "$repo/.git" ]] || return 0
  [[ -n "$(git -C "$repo" status --porcelain 2>/dev/null)" ]] &&
    echo "!! $name has uncommitted changes — they will NOT be deployed"

  local head remote_head
  head="$(git -C "$repo" rev-parse HEAD 2>/dev/null)"
  # GIT_TERMINAL_PROMPT=0: never block the deploy on a credential prompt.
  remote_head="$(GIT_TERMINAL_PROMPT=0 git -C "$repo" \
                 -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=10 \
                 ls-remote "$url" refs/heads/main 2>/dev/null | awk '{print $1; exit}')"
  if [[ -z "$remote_head" ]]; then
    echo "?? $name: could not reach $url — cannot tell if HEAD is pushed"
  elif [[ "$head" != "$remote_head" ]]; then
    echo "!! $name HEAD ${head:0:7} != remote main ${remote_head:0:7} — push first"
  fi
  return 0
}

preflight "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" ystocker \
  "$(git -C "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" remote get-url origin 2>/dev/null)"
[[ $WITH_TA -eq 1 ]] &&
  preflight "$HOME/workspace/TradingAgents" TradingAgents "$TA_REMOTE"

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
# Every app gets a real restart. NOT `kill -HUP`, which is what the deploy
# one-liner in CLAUDE.md used to do and which silently shipped stale code:
# gunicorn`s HUP handler calls Application.reload(), and that re-reads the
# *config file* only. Under --preload the WSGI app was imported once by the
# master at ExecStart, so HUP re-forks workers from that same already-imported
# module state and new Python never loads. Measured on this box: a route added
# to yhome/routes.py returned 404 after HUP and 200 after restart. Templates did
# refresh, because a forked worker starts with an empty Jinja cache -- which is
# exactly what made the bug so easy to miss.
#
# ystocker additionally must not be HUPed: it runs --preload, so create_app()
# and its cache-warming, 13F, heatmap and email threads live in the master, and
# HUP cannot stop threads a previous import started.
for svc in ystocker yplanner yplanter yhome ytracker ypay yimage ybg; do
  sudo systemctl restart "$svc"
done
sleep 10
for svc in ystocker yplanner yplanter yhome ytracker ypay yimage ybg; do
  printf "   %-10s %s\n" "$svc" "$(systemctl is-active $svc)"
done
printf "   http     agents=%s multiples=%s planner=%s home=%s\n" \
  "$(curl -s -o /dev/null -w "%{http_code}" https://stock.li-family.us/agents)" \
  "$(curl -s -o /dev/null -w "%{http_code}" https://stock.li-family.us/multiples)" \
  "$(curl -s -o /dev/null -w "%{http_code}" https://planner.li-family.us/)" \
  "$(curl -s -o /dev/null -w "%{http_code}" https://home.li-family.us/)"
'

run_remote "$STEPS"
