#!/usr/bin/env bash
# take_screenshots.sh
# -----------------------------------------------------------
# Captures screenshots of every yStocker page and saves them
# to ystocker/static/img/guide/ for use in the Guide page.
#
# Usage:
#   chmod +x take_screenshots.sh
#   ./take_screenshots.sh
#
# Requires: Google Chrome installed at the default Mac path.
# The dev server must be running at http://127.0.0.1:5000
# -----------------------------------------------------------

set -euo pipefail

OUT="$(dirname "$0")/ystocker/static/img/guide"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BASE="http://127.0.0.1:5000"
WIDTH=1440
HEIGHT=900

mkdir -p "$OUT"

# Check server is up
if ! curl -sf "$BASE/" -o /dev/null; then
  echo "❌  Dev server not reachable at $BASE — start it first."
  exit 1
fi

echo "📸  Taking screenshots → $OUT"

# Kill any leftover debug Chrome
pkill -f "remote-debugging-port=9223" 2>/dev/null || true
sleep 0.5

# Start headless Chrome with CDP
"$CHROME" \
  --headless=old \
  --remote-debugging-port=9223 \
  --disable-gpu \
  --no-sandbox \
  --disable-dev-shm-usage \
  --window-size="${WIDTH},${HEIGHT}" \
  about:blank &
CPID=$!
trap "kill $CPID 2>/dev/null || true" EXIT
sleep 3

python3 - <<PYEOF
import json, time, base64, os, sys, urllib.request

OUT  = "$OUT"
BASE = "$BASE"

pages = [
    ("/",                      "home.png"),
    ("/sector/Mega%20Cap%20Tech", "sector.png"),
    ("/lookup",                "lookup.png"),
    ("/13f",                   "thirteenf.png"),
    ("/fed",                   "fed.png"),
    ("/groups",                "groups.png"),
]

# Get WS target
for attempt in range(6):
    try:
        data = json.loads(urllib.request.urlopen("http://localhost:9223/json", timeout=3).read())
        ws_url = data[0]["webSocketDebuggerUrl"]
        break
    except Exception:
        time.sleep(1)
else:
    print("❌  Could not connect to Chrome CDP")
    sys.exit(1)

import websocket   # pip install websocket-client if missing

ws = websocket.create_connection(ws_url, timeout=30)
_id = 0

def cdp(method, **params):
    global _id
    _id += 1
    ws.send(json.dumps({"id": _id, "method": method, "params": params}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == _id:
            return msg.get("result", {})

# Set viewport
cdp("Emulation.setDeviceMetricsOverride",
    width=$WIDTH, height=$HEIGHT, deviceScaleFactor=1, mobile=False)

for path, fname in pages:
    url = BASE + path
    print(f"  {url} …", end=" ", flush=True)
    cdp("Page.navigate", url=url)
    time.sleep(3.5)
    result = cdp("Page.captureScreenshot", format="png", fromSurface=True, captureBeyondViewport=True)
    img = base64.b64decode(result.get("data", ""))
    if img:
        dest = os.path.join(OUT, fname)
        with open(dest, "wb") as f:
            f.write(img)
        print(f"✓  ({len(img)//1024} KB)")
    else:
        print("✗  no data")

ws.close()
PYEOF

echo ""
echo "✅  Done. Files in $OUT:"
ls -lh "$OUT/"
