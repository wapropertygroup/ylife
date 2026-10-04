#!/usr/bin/env bash
# release.sh — tag one deploy and publish it as a GitHub Release.
#
#   bash deploy/release.sh <ystocker-sha> [<tradingagents-sha | ->] [<health line>]
#
# deploy.sh calls this once a deploy has landed; it is also safe to run by hand.
# Every step is best-effort and the script always exits 0: a deploy that shipped
# must never be reported as failed because GitHub could not be reached.
#
# Two layers, because they need different credentials:
#
# 1. A git tag on the deployed commit, deploy-YYYY-MM-DD-HHMM (UTC), annotated
#    with the release notes: the commits since the previous deploy tag, the
#    TradingAgents commit that shipped with it, and the health check. Pushing it
#    needs only what `git push` already uses.
# 2. A GitHub Release for every deploy tag that has none, oldest first, so the
#    newest ends up marked latest. Title and notes are the tag's own subject and
#    body, so a release published later says exactly what one published on the
#    day would have. This needs `gh` logged in to github.com. On this laptop gh
#    is logged in to another host only (GH_HOST), so until
#    `gh auth login --hostname github.com` the tags carry the history, and the
#    next deploy after that login publishes the releases they are owed.
#
# A deploy that shipped nothing new (its commit already carries a deploy tag) is
# not a new release; any releases still owed are published all the same.
set -u

SHA="${1:-}"
TA_SHA="${2:--}"
HEALTH="${3:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GIT_TERMINAL_PROMPT=0

say() { echo "== release: $*"; }
g() { git -C "$ROOT" "$@"; }
# Network calls give up on a stalled connection rather than hold the deploy's
# last step open, as deploy.sh's preflight does.
gn() { git -C "$ROOT" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=20 "$@"; }

if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  say "no deployed commit to tag (got '${SHA}'), skipped"
  exit 0
fi

url="$(g remote get-url origin 2>/dev/null || true)"
slug="$(printf '%s' "$url" | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##')"
if [[ "$url" != *github.com* || ! "$slug" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
  say "origin is not a github.com repository ($url), skipped"
  exit 0
fi

# GitHub Releases for every deploy tag that has none, oldest first.
publish_owed() {
  if ! command -v gh >/dev/null 2>&1; then
    say "gh is not installed: the tag is the record, no GitHub Release"
    return 0
  fi
  if ! GH_HOST=github.com gh auth status --hostname github.com >/dev/null 2>&1; then
    say "GitHub Release not published: gh is not logged in to github.com."
    say "  Run once:  gh auth login --hostname github.com"
    say "  The next deploy then publishes a release for every deploy tag since."
    return 0
  fi
  local have t title body n=0
  if ! have="$(GH_HOST=github.com gh release list --repo "$slug" --limit 1000 \
                 --json tagName --jq '.[].tagName' 2>/dev/null)"; then
    say "could not list the repository's releases, none published"
    return 0
  fi
  gn fetch -q origin 'refs/tags/deploy-*:refs/tags/deploy-*' 2>/dev/null || true
  # Tag names are UTC timestamps, so name order is time order.
  while read -r t; do
    [[ -z "$t" ]] && continue
    grep -qxF "$t" <<<"$have" && continue
    title="$(g tag -l --format='%(contents:subject)' "$t")"
    body="$(g tag -l --format='%(contents:body)' "$t")"
    if GH_HOST=github.com gh release create "$t" --repo "$slug" --verify-tag \
         --title "${title:-$t}" --notes "${body:-Deployed.}" >/dev/null 2>&1; then
      say "published https://github.com/$slug/releases/tag/$t"
      n=$((n + 1))
    else
      say "could not publish $t"
    fi
  done < <(g tag -l 'deploy-*' | LC_ALL=C sort)
  [[ $n -eq 0 ]] && say "no GitHub Release owed"
  return 0
}

# The deployed commit has to be in this clone to be tagged; a peer may have
# pushed it from elsewhere.
if ! g cat-file -e "$SHA^{commit}" 2>/dev/null; then
  gn fetch -q origin main 2>/dev/null || true
fi
if ! g cat-file -e "$SHA^{commit}" 2>/dev/null; then
  say "commit ${SHA:0:7} is not in this clone and could not be fetched, skipped"
  exit 0
fi

# Deploy tags on the remote, as "<commit> <tag>" in name (= time) order. Read from
# the remote, not local refs: a peer's deploy may have tagged since this clone
# last fetched. A read that fails is not "no tags", or every deploy during an
# outage would re-tag a commit that already has one.
if ! raw="$(gn ls-remote --tags origin 'deploy-*' 2>/dev/null)"; then
  say "could not read tags from origin, nothing tagged"
  exit 0
fi
remote_tags="$(printf '%s\n' "$raw" | awk '$2 ~ /\^\{\}$/ {
  t = $2; sub(/^refs\/tags\//, "", t); sub(/\^\{\}$/, "", t); print $1, t }' | LC_ALL=C sort -k2)"

existing="$(printf '%s\n' "$remote_tags" | awk -v s="$SHA" '$1 == s {print $2; exit}')"
if [[ -n "$existing" ]]; then
  say "${SHA:0:7} is already released as $existing, nothing new"
  publish_owed
  exit 0
fi

prev_line="$(printf '%s\n' "$remote_tags" | awk 'NF == 2' | tail -1)"
prev_sha="${prev_line%% *}"
prev_tag="${prev_line#* }"
[[ -z "$prev_line" ]] && prev_sha="" && prev_tag=""
if [[ -n "$prev_sha" ]] && ! g cat-file -e "$prev_sha^{commit}" 2>/dev/null; then
  gn fetch -q origin "refs/tags/$prev_tag:refs/tags/$prev_tag" 2>/dev/null || true
fi

stamp="$(date -u +%Y-%m-%d-%H%M)"
tag="deploy-$stamp"
n=2
while printf '%s\n' "$remote_tags" | awk '{print $2}' | grep -qxF "$tag" \
      || g rev-parse -q --verify "refs/tags/$tag" >/dev/null; do
  tag="deploy-$stamp-$n"
  n=$((n + 1))
done

notes="$(mktemp -t ystocker-release.XXXXXX)"
trap 'rm -f "$notes"' EXIT
{
  echo "Deploy $(date -u '+%Y-%m-%d %H:%M UTC') · ${SHA:0:7}"
  echo
  echo "Live on stock.li-family.us and trade-agents.com."
  echo
  if [[ -n "$prev_sha" ]] && g cat-file -e "$prev_sha^{commit}" 2>/dev/null; then
    echo "ystocker ${SHA:0:7}, since $prev_tag (${prev_sha:0:7}):"
    echo
    g log --no-merges --format='- %s (%h)' "$prev_sha..$SHA"
  else
    echo "ystocker ${SHA:0:7}. The first tagged deploy; its last 20 commits:"
    echo
    g log --no-merges --format='- %s (%h)' -20 "$SHA"
  fi
  echo
  if [[ "$TA_SHA" =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "TradingAgents ${TA_SHA:0:7} shipped with it."
  else
    echo "TradingAgents was not part of this deploy."
  fi
  if [[ -n "$HEALTH" ]]; then
    echo
    echo "After the restart: $HEALTH"
  fi
} > "$notes"

# Unsigned whatever the global config says: commits here are signed with an
# x509 helper that can wait on a prompt, and a deploy must not hang at the end.
if ! g -c tag.gpgSign=false tag -a "$tag" "$SHA" -F "$notes" 2>/dev/null; then
  say "could not create tag $tag"
  exit 0
fi
if ! gn push -q origin "refs/tags/$tag" 2>/dev/null; then
  g tag -d "$tag" >/dev/null 2>&1 || true
  say "could not push tag $tag, nothing recorded"
  exit 0
fi
say "tagged ${SHA:0:7} as $tag"
publish_owed
exit 0
