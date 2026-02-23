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
# The server must be reachable at BASE (default: https://stock.li-family.us)
# -----------------------------------------------------------

set -euo pipefail

OUT="$(dirname "$0")/ystocker/static/img/guide"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BASE="https://stock.li-family.us"
WIDTH=1440
HEIGHT=900
PORT=9223

mkdir -p "$OUT"

# Check server is up
if ! curl -sf "$BASE/" -o /dev/null; then
  echo "❌  Server not reachable at $BASE — check BASE or start the server."
  exit 1
fi

echo "📸  Taking screenshots → $OUT"

# Kill any leftover debug Chrome
pkill -f "remote-debugging-port=$PORT" 2>/dev/null || true
sleep 0.5

# Start headless Chrome with CDP
"$CHROME" \
  --headless=new \
  --remote-debugging-port="$PORT" \
  --remote-allow-origins="*" \
  --disable-gpu \
  --no-sandbox \
  --no-first-run \
  --user-data-dir="/tmp/chrome-ystocker-profile" \
  --disable-dev-shm-usage \
  --disable-background-networking \
  --disable-component-update \
  --disable-extensions \
  --disable-features=ExtensionsToolbarMenu \
  --log-level=3 \
  --window-size="${WIDTH},${HEIGHT}" \
  about:blank >/dev/null 2>&1 &

CPID=$!
trap "kill $CPID 2>/dev/null || true" EXIT
sleep 4

python3 - <<PYEOF
import json, time, base64, os, sys, urllib.request

OUT  = "$OUT"
BASE = "$BASE"
PORT = $PORT

routes = [
    ("/",                          "home.png"),
    ("/sector/Tech",               "sector.png"),
    ("/lookup",                    "lookup.png"),
    ("/13f",                       "thirteenf.png"),
    ("/fed",                       "fed.png"),
    ("/groups",                    "groups.png"),
]

def fetch_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())

# Get WS target (prefer a normal page tab, not an extension background page)
ws_url = None
for attempt in range(30):
    try:
        data = fetch_json(f"http://localhost:{PORT}/json", timeout=3)
        targets = [t for t in data if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if targets:
            target = next((t for t in targets if t.get("url") == "about:blank"), targets[0])
            ws_url = target["webSocketDebuggerUrl"]
            break
    except Exception:
        pass
    time.sleep(1)

if not ws_url:
    print("❌  Could not find a suitable page target on Chrome CDP")
    sys.exit(1)

try:
    import websocket  # pip install websocket-client if missing
except Exception:
    print("❌  Missing dependency: websocket-client")
    print("    Install it with: pip3 install websocket-client")
    sys.exit(1)

ws = websocket.create_connection(ws_url, timeout=60)
_id = 0

def cdp(method, **params):
    global _id
    _id += 1
    ws.send(json.dumps({"id": _id, "method": method, "params": params}))
    target_id = _id
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == target_id:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result", {})

def navigate_and_wait(url, wait=2, timeout=60):
    global _id
    _id += 1
    nav_id = _id
    ws.send(json.dumps({"id": nav_id, "method": "Page.navigate", "params": {"url": url}}))

    deadline = time.time() + timeout
    got_nav_ack = False

    # Drain events until we see the navigate ack, then wait a bit for SPA rendering
    while time.time() < deadline:
        ws.settimeout(max(1, deadline - time.time()))
        try:
            msg = json.loads(ws.recv())
        except Exception:
            break
        if msg.get("id") == nav_id:
            got_nav_ack = True
            break

    ws.settimeout(60)
    if not got_nav_ack:
        raise TimeoutError(f"Timed out navigating to {url}")

    time.sleep(wait)

# Enable domains we use
cdp("Page.enable")

# Set viewport
cdp("Emulation.setDeviceMetricsOverride",
    width=$WIDTH, height=$HEIGHT, deviceScaleFactor=1, mobile=False)

for path, fname in routes:
    url = BASE + path
    print(f"  {url} …", end=" ", flush=True)
    try:
        navigate_and_wait(url, wait=4)
        result = cdp("Page.captureScreenshot", format="png", fromSurface=True)
        img = base64.b64decode(result.get("data", "") or b"")
        if not img:
            print("✗  no data")
            continue

        dest = os.path.join(OUT, fname)
        with open(dest, "wb") as f:
            f.write(img)
        print(f"✓  ({len(img)//1024} KB)")
    except Exception as e:
        print(f"✗  {e}")

ws.close()
PYEOF

echo ""
echo "✅  Done. Files in $OUT:"
ls -lh "$OUT/"