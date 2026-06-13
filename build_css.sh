#!/usr/bin/env bash
# build_css.sh — Compile Tailwind CSS once and distribute to all apps.
# Run this locally whenever you add new Tailwind classes to any template,
# then commit the updated tailwind.css files together with your template changes.
#
# Requires: Node.js + npm (https://nodejs.org) — or pytailwindcss as a fallback.
#
# Usage:
#   bash build_css.sh
#
set -euo pipefail
cd "$(dirname "$0")"

APPS=(ystocker yplanner yplanter ytracker ypay yimage ybg)

# ── Build ────────────────────────────────────────────────────────────────────
mkdir -p shared

echo "Building Tailwind CSS..."
if command -v npx >/dev/null 2>&1; then
  # Preferred: use the official Tailwind CLI via npx (Node.js required)
  npx --yes tailwindcss@3 \
    --config tailwind.config.js \
    -i shared/input.css \
    -o shared/tailwind.css \
    --minify
elif command -v python3 >/dev/null 2>&1; then
  # Fallback: pytailwindcss (pip install pytailwindcss — downloads standalone binary)
  python3 -m pytailwindcss \
    --config tailwind.config.js \
    -i shared/input.css \
    -o shared/tailwind.css \
    --minify
else
  echo "ERROR: Neither npx nor python3 found. Install Node.js or pytailwindcss." >&2
  exit 1
fi

SIZE=$(wc -c < shared/tailwind.css | tr -d ' ')
echo "  Built: ${SIZE} bytes → shared/tailwind.css"

# ── Distribute to each app's static/css/ directory ───────────────────────────
echo "Distributing..."
for app in "${APPS[@]}"; do
  dest="${app}/static/css/tailwind.css"
  mkdir -p "${app}/static/css"
  cp shared/tailwind.css "$dest"
  echo "  → $dest"
done

echo ""
echo "✓ Done. Commit shared/tailwind.css and */static/css/tailwind.css with your changes."
