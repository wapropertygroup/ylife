#!/usr/bin/env bash
# Regenerate ystocker/data/gics_sp500.json on the box and bring it back.
#
#   bash deploy/gics-snapshot.sh
#
# The snapshot is built from Wikipedia's S&P 500 constituent list and SPY's
# daily holdings file (www.ssga.com). Where this machine can reach both,
# `./venv/bin/python -m ystocker.gics --write-snapshot` does the same thing
# locally; this exists for where it cannot — a sandboxed session, a restrictive
# proxy — since the box reaches both.
#
# It runs the *deployed* ystocker.gics, so push and deploy first if you have
# changed that module. The file is written to /tmp on the box, never into
# /opt/ystocker: the deploy resets that checkout hard, so a file written there
# would dirty it until the next deploy silently threw it away.
#
# It changes nothing but the local file. Review the diff (one member per line),
# run the tests, commit, deploy — the snapshot also sets breadth.py's S&P 500
# universe, so both move together.
set -euo pipefail
cd "$(dirname "$0")/.."

SSM_REGION="${AWS_REGION:-us-west-2}"
INSTANCE_NAME_TAG="${YSTOCKER_INSTANCE_TAG:-ystocker-instance}"
OUT="ystocker/data/gics_sp500.json"

# By tag, never a pinned id: see CLAUDE.md "EC2 Instance".
INSTANCE="$(aws ec2 describe-instances --region "$SSM_REGION" \
    --filters "Name=tag:Name,Values=$INSTANCE_NAME_TAG" "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[0].InstanceId' --output text | head -n1 | tr -d '[:space:]')"
if [[ -z "$INSTANCE" || "$INSTANCE" == "None" ]]; then
  echo "no running instance tagged Name=$INSTANCE_NAME_TAG in $SSM_REGION" >&2
  exit 1
fi
echo "== box: $INSTANCE"

# stdout carries only the file, gzipped and base64'd onto one line: SSM keeps
# 24,000 characters of stdout, and the snapshot is ~58 KB raw but ~16 KB this
# way. The module's own log (what joined, what left) goes to stderr.
REMOTE='set -e; cd /opt/ystocker
/opt/ystocker/venv/bin/python -m ystocker.gics --write-snapshot --out /tmp/gics_sp500.json 1>&2
gzip -c /tmp/gics_sp500.json | base64 -w0'
PARAMS="$(python3 -c 'import json,sys; print(json.dumps({"commands": [sys.argv[1]]}))' "$REMOTE")"

CMD="$(aws ssm send-command --instance-ids "$INSTANCE" --region "$SSM_REGION" \
        --document-name AWS-RunShellScript --timeout-seconds 300 \
        --parameters "$PARAMS" --query 'Command.CommandId' --output text)"
STATUS=Pending
for _ in $(seq 1 60); do
  sleep 3
  STATUS="$(aws ssm get-command-invocation --command-id "$CMD" --instance-id "$INSTANCE" \
            --region "$SSM_REGION" --query Status --output text 2>/dev/null || echo Pending)"
  case "$STATUS" in Success|Failed|TimedOut|Cancelled) break ;; esac
done

aws ssm get-command-invocation --command-id "$CMD" --instance-id "$INSTANCE" \
    --region "$SSM_REGION" --query StandardErrorContent --output text | sed 's/^/   box: /'
if [[ "$STATUS" != "Success" ]]; then
  echo "== failed on the box ($STATUS); $OUT is unchanged" >&2
  exit 1
fi

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
aws ssm get-command-invocation --command-id "$CMD" --instance-id "$INSTANCE" \
    --region "$SSM_REGION" --query StandardOutputContent --output text \
  | tr -d '[:space:]' | base64 --decode | gunzip > "$TMP"

# Checked with the same loader the app uses before it replaces anything, so a
# truncated transfer cannot overwrite a good snapshot with half of one.
python3 - "$TMP" "$OUT" <<'PY'
import json, sys
new = json.load(open(sys.argv[1]))
assert new.get("schema") == 1 and len(new.get("members", {})) >= 450, "snapshot looks incomplete"
try:
    old = json.load(open(sys.argv[2]))["members"]
except (OSError, ValueError, KeyError):
    old = {}
joined, left = sorted(set(new["members"]) - set(old)), sorted(set(old) - set(new["members"]))
print(f"== {len(new['members'])} members, weights as of {new['weights_asof']}")
print(f"   joined: {', '.join(joined) or 'none'}")
print(f"   left:   {', '.join(left) or 'none'}")
PY
mv "$TMP" "$OUT"
trap - EXIT
echo "== written to $OUT — review with: git diff --stat $OUT"
