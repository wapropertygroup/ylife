# Li Family Apps Monorepo

A Flask-based monorepo hosting 5 web applications for the Li family at **li-family.us**.

## Apps

| App | Dir | Port | URL | Purpose |
|-----|-----|------|-----|---------|
| **yStocker** | `ystocker/` | 5000 | stock.li-family.us | Stock research, valuation, Fed, 13F, forecasts |
| **yPlanner** | `yplanner/` | 5001 | planner.li-family.us | AI trip planning with Google Maps |
| **yPlanter** | `yplanter/` | 5002 | plant.li-family.us | PNW gardening guide (50+ plants) |
| **yHome** | `yhome/` | 5003 | li-family.us | Landing page / navigation hub |
| **yTracker** | `ytracker/` | 5004 | tracker.li-family.us | Multi-store price tracking & alerts |

## Quick Start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements_stocker.txt   # or requirements_{planner,planter,tracker,home}.txt
python run/run_stocker.py                 # starts on http://127.0.0.1:5000
```

Optional: create `.env` with `GEMINI_API_KEY=...` for AI features.

## Repository Structure

```
run/                    Entry points (run_stocker.py, run_planner.py, etc.)
deploy/                 CloudFormation, deploy.sh, sync-ssm.sh
cache/                  On-disk JSON caches (auto-created, gitignored)
requirements_*.txt      Per-app Python dependencies

ystocker/               Stock research app
  __init__.py           App factory, PEER_GROUPS config, YT_CHANNELS
  routes.py             All routes + API endpoints (5200+ lines)
  data.py               Yahoo Finance data fetching
  fed.py                Federal Reserve H.4.1 from FRED
  sec13f.py             SEC EDGAR 13F institutional holdings
  forecast.py           Prophet / ARIMA / Linear price forecasting
  charts.py             Matplotlib/Seaborn chart generation (base64 PNG)
  heatmap_meta.py       S&P 500 metadata for market heatmap
  templates/            17 Jinja2 templates
  static/               CSS, i18n.js, favicon

yhome/                  Landing page (minimal: __init__.py, routes.py, 1 template)
yplanner/               Trip planner (routes.py, DynamoDB, Google/Apple Sign-In)
yplanter/               Garden guide (routes.py, plants_db.py, DynamoDB)
ytracker/               Price tracker (routes.py, scraper.py, DynamoDB)
```

## Architecture Patterns

- **Caching**: Two-tier (in-memory dict + on-disk JSON) with TTLs (8h stock, 24h Fed/13F)
- **Thread safety**: `threading.Lock` on all cache reads/writes
- **Atomic writes**: Temp file + `os.replace()` for crash-safe disk persistence
- **Background tasks**: Daemon threads for cache warming, 13F refresh, heatmap snapshots, email broadcast
- **Auth**: Google/Apple Sign-In (yPlanner, yTracker); public (yStocker, yPlanter, yHome)
- **Storage**: DynamoDB (yPlanner, yPlanter, yTracker); file-based JSON cache (yStocker)
- **Secrets**: AWS SSM Parameter Store (prod) / `.env` (dev)
- **i18n**: English + Simplified Chinese via `i18n.js` in each app

## Key APIs (yStocker)

| Endpoint | Description |
|----------|-------------|
| `GET /api/ticker/<t>` | Single stock metrics |
| `GET /api/history/<t>` | Price + PE history + options data |
| `GET /api/financials/<t>` | Income statement (3y actual + 2y estimates) |
| `GET /api/forecast/<t>` | 6-month price forecast (Prophet/ARIMA/Linear) |
| `GET /api/fed` | Federal Reserve H.4.1 balance sheet |
| `GET /api/13f/<fund>` | Institutional 13F holdings |
| `GET /api/13f/ticker/<t>` | Which funds hold this stock |
| `POST /api/history/<t>/explain` | AI chart analysis (SSE stream) |
| `GET /api/news/<t>` | Recent news articles |
| `GET /api/markets` | Broad market indices + commodities |

## Production Deployment

### Deploy all 5 apps to EC2

```bash
bash deploy/deploy.sh -i ~/Downloads/my-key-pair.pem
```

This single command SSHs into the EC2 instance and:
1. `git fetch` + `git reset --hard origin/main`
2. Installs pip dependencies for all 5 apps
3. Creates/updates systemd Gunicorn services (ports 8000-8004)
4. Configures nginx reverse proxy vhosts
5. Provisions Let's Encrypt SSL via certbot
6. Warms up each app with a health-check curl

The `-i` flag specifies the SSH key. If omitted, the script auto-detects from `~/.ssh/*.pem`.

### Deploy infrastructure (first time only)

```bash
aws cloudformation deploy --template-file deploy/cloudformation.yaml \
  --stack-name ystocker --parameter-overrides KeyName=my-key-pair \
  --capabilities CAPABILITY_NAMED_IAM
```

### Sync secrets to AWS SSM

```bash
bash deploy/sync-ssm.sh          # reads .env, writes to SSM Parameter Store
bash deploy/sync-ssm.sh --dry-run  # preview without writing
```

### Production architecture

```
nginx (port 80/443, SSL)
  ├─ stock.li-family.us    → gunicorn :8000 (ystocker)
  ├─ planner.li-family.us  → gunicorn :8001 (yplanner)
  ├─ plant.li-family.us    → gunicorn :8002 (yplanter)
  ├─ home.li-family.us     → gunicorn :8003 (yhome)
  └─ tracker.li-family.us  → gunicorn :8004 (ytracker)
```

Each app runs as a systemd service with 2 Gunicorn workers, 120s timeout, auto-restart.

## Code Conventions

- Python 3.12+ with `from __future__ import annotations`
- Modern type hints: `dict[str, list[str]]` not `Dict[str, List[str]]`
- All modules have docstrings and structured logging (`logging.getLogger(__name__)`)
- Private helpers prefixed with `_`
- No bare `except:` — always catch specific exceptions
- Templates extend `base.html` with Tailwind CSS
