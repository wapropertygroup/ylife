#!/usr/bin/env bash
# deploy/sync-ssm.sh — Push local .env secrets to AWS SSM Parameter Store
# Usage: deploy/sync-ssm.sh [--dry-run]
#
# Reads .env from the repo root and uploads each key as a SecureString
# under the appropriate SSM path. Skips empty values.
set -eo pipefail

REGION="${AWS_REGION:-us-west-2}"
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: .env not found at $ENV_FILE"
  exit 1
fi

# ── Mapping: ENV_VAR → SSM path ─────────────────────────────────────────────
# Add new mappings here when you add new env vars or apps.
get_ssm_path() {
  case "$1" in
    GEMINI_API_KEY)       echo "/ystocker/GEMINI_API_KEY" ;;
    YOUTUBE_API_KEY)      echo "/ystocker/YOUTUBE_API_KEY" ;;
    SES_FROM_EMAIL)       echo "/ystocker/SES_FROM_EMAIL" ;;
    GOOGLE_MAPS_API_KEY)  echo "/yplanner/GOOGLE_MAPS_API_KEY" ;;
    GOOGLE_CLIENT_ID)     echo "/yplanner/GOOGLE_CLIENT_ID" ;;
    APPLE_SERVICE_ID)     echo "/yplanner/APPLE_SERVICE_ID" ;;
    *)                    echo "" ;;
  esac
}

# ── Parse .env and upload ────────────────────────────────────────────────────
uploaded=0
skipped=0

while IFS= read -r line || [[ -n "$line" ]]; do
  # Skip comments and blank lines
  [[ -z "$line" || "$line" == \#* ]] && continue

  # Parse KEY=VALUE
  if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*) ]]; then
    key="${BASH_REMATCH[1]}"
    val="${BASH_REMATCH[2]}"
  else
    continue
  fi

  # Strip surrounding quotes
  if [[ "$val" =~ ^\"(.*)\"$ ]]; then val="${BASH_REMATCH[1]}"; fi
  if [[ "$val" =~ ^\'(.*)\'$ ]]; then val="${BASH_REMATCH[1]}"; fi

  # Look up SSM path
  ssm_path=$(get_ssm_path "$key")
  if [[ -z "$ssm_path" ]]; then
    echo "  SKIP  $key (no SSM mapping)"
    skipped=$((skipped + 1))
    continue
  fi

  # Skip empty values
  if [[ -z "$val" ]]; then
    echo "  SKIP  $key → $ssm_path (empty value)"
    skipped=$((skipped + 1))
    continue
  fi

  # Mask value for display
  if [[ ${#val} -gt 12 ]]; then
    display="${val:0:8}..."
  else
    display="$val"
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [DRY] $key → $ssm_path = $display"
  else
    aws ssm put-parameter \
      --name "$ssm_path" \
      --value "$val" \
      --type SecureString \
      --overwrite \
      --region "$REGION" \
      --no-cli-pager > /dev/null
    echo "  PUT   $key → $ssm_path = $display"
  fi
  uploaded=$((uploaded + 1))
done < "$ENV_FILE"

# ── Auto-generate YPLANNER_SECRET_KEY if missing ─────────────────────────────
YPLANNER_KEY_PATH="/yplanner/YPLANNER_SECRET_KEY"
if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY] YPLANNER_SECRET_KEY → $YPLANNER_KEY_PATH (auto-generated if missing)"
else
  if ! aws ssm get-parameter --name "$YPLANNER_KEY_PATH" --region "$REGION" --no-cli-pager &>/dev/null; then
    secret=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    aws ssm put-parameter \
      --name "$YPLANNER_KEY_PATH" \
      --value "$secret" \
      --type SecureString \
      --region "$REGION" \
      --no-cli-pager > /dev/null
    echo "  NEW   YPLANNER_SECRET_KEY → $YPLANNER_KEY_PATH (auto-generated)"
    uploaded=$((uploaded + 1))
  else
    echo "  OK    YPLANNER_SECRET_KEY → $YPLANNER_KEY_PATH (already exists)"
  fi
fi

echo ""
echo "Done: $uploaded uploaded, $skipped skipped (region: $REGION)"
if [[ "$DRY_RUN" == "true" ]]; then
  echo "This was a dry run. Remove --dry-run to upload."
fi
