# Li Family Apps

A monorepo of Flask web applications built for the Li family — covering stock
research, trip planning, gardening, and price tracking.

**Live at [li-family.us](https://li-family.us)**

| App | URL | Description |
|-----|-----|-------------|
| **yStocker** | [stock.li-family.us](https://stock.li-family.us) | Stock research & portfolio analysis |
| **yPlanner** | [planner.li-family.us](https://planner.li-family.us) | AI-powered trip planner |
| **yPlanter** | [plant.li-family.us](https://plant.li-family.us) | Pacific Northwest gardening guide |
| **yTracker** | [tracker.li-family.us](https://tracker.li-family.us) | Multi-store price tracker with alerts |
| **yHome** | [li-family.us](https://li-family.us) | Landing page / app directory |

---

## Project structure

```
ystocker/                       <- git repository root
|
+-- run/                        <- entry points (one per app)
|   +-- run_stocker.py          <- yStocker dev server
|   +-- run_home.py             <- yHome dev server
|   +-- run_planner.py          <- yPlanner dev server
|   +-- run_planter.py          <- yPlanter dev server
|   +-- run_tracker.py          <- yTracker dev server
|
+-- ystocker/                   <- stock research app
|   +-- __init__.py             <- Flask factory + peer-group config
|   +-- routes.py               <- URL routes, JSON API, background jobs
|   +-- data.py                 <- Yahoo Finance data fetching
|   +-- fed.py                  <- Federal Reserve FRED data
|   +-- sec13f.py               <- SEC EDGAR 13F institutional holdings
|   +-- forecast.py             <- Prophet / ARIMA / linear price forecasting
|   +-- charts.py               <- matplotlib / seaborn chart generation
|   +-- heatmap_meta.py         <- S&P 500 metadata for the market heatmap
|   +-- templates/              <- 17 Jinja2 templates
|   |   +-- base.html           <- shared navbar + layout
|   |   +-- index.html          <- home (sector cards, cross-sector charts)
|   |   +-- sector.html         <- per-sector detail (charts + data table)
|   |   +-- history.html        <- single-ticker deep dive
|   |   +-- lookup.html         <- ticker search + discover by sector
|   |   +-- groups.html         <- manage peer groups
|   |   +-- fed.html            <- Federal Reserve balance sheet charts
|   |   +-- thirteenf.html      <- institutional 13F holdings
|   |   +-- heatmap.html        <- S&P 500 market heatmap
|   |   +-- markets.html        <- broad market overview
|   |   +-- daily_report.html   <- email digest template
|   |   +-- guide.html          <- help / documentation
|   |   +-- videos.html         <- curated YouTube finance feed
|   |   +-- warming.html        <- cache warming screen
|   |   +-- error.html          <- error page
|   |   +-- contact.html        <- contact form
|   |   +-- unsubscribe.html    <- email unsubscribe
|   +-- static/
|       +-- css/style.css
|       +-- i18n.js             <- English / Simplified Chinese translations
|
+-- yhome/                      <- landing page app
+-- yplanner/                   <- trip planner app
+-- yplanter/                   <- gardening app
+-- ytracker/                   <- price tracker app
|
+-- cache/                      <- persistent on-disk cache (auto-created)
|   +-- ticker_cache.json       <- stock metrics (8 h TTL)
|   +-- peer_groups.json        <- user-managed peer groups
|   +-- fed_cache.json          <- Federal Reserve data (24 h TTL)
|   +-- sec13f_cache.json       <- SEC 13F holdings (24 h TTL)
|
+-- deploy/                     <- deployment configs
+-- requirements_stocker.txt    <- yStocker Python dependencies
+-- cloudformation.yaml         <- AWS deployment template
+-- .env                        <- local secrets (not committed)
```

---

## Quick start (yStocker)

### 1. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements_stocker.txt
```

### 3. Configure API keys (optional)

AI explanations require a Google Gemini API key. Create a `.env` file in the
repository root:

```
GEMINI_API_KEY=your_key_here
```

Without this key the app runs normally; only the AI explanation panels are
disabled.

### 4. Run the development server

```bash
python run/run_stocker.py
```

Open your browser at **http://127.0.0.1:5000**.

---

## yStocker features

### Peer group valuation dashboard

The home page and per-sector pages display forward PE, TTM PE, PEG ratios,
analyst price targets, upside %, EPS growth, and market cap for every ticker in
each peer group. Charts include bar comparisons, a valuation scatter plot
(Forward PE vs analyst upside), and a colour-coded heatmap.

### Single-ticker analysis (`/history/<ticker>`)

- Historical PE / PEG / price charts (configurable period: 1 month - 5 years)
- Options wall - aggregated call/put open interest across all expirations to
  visualise support and resistance levels
- Institutional holders ranked by portfolio weight, value, and change
- AI-powered chart explanation (streams via Server-Sent Events, English and
  Chinese supported)
- Recent news with importance scoring

### Market heatmap (`/heatmap`)

S&P 500 sector heatmap with ~105 large-cap stocks. Tile size reflects market
cap; colour reflects daily price change. Auto-snapshots at market close on
weekdays.

### Broad market overview (`/markets`)

Indices, commodities, crypto, gold ratios, Fear & Greed index, and put/call
ratio - all on a single page.

### Price forecasting (`/api/forecast/<ticker>`)

Multi-model 6-month forward forecast using Prophet, ARIMA (AutoARIMA via
pmdarima), and linear regression. Includes 80% confidence intervals.

### Federal Reserve dashboard (`/fed`)

Weekly H.4.1 data pulled directly from FRED (no API key required). Charts
cover Total Assets, Treasury Holdings, MBS, Reserve Balances, ON RRP, Treasury
General Account, Currency in Circulation, and Fed Loans. AI explanations
summarise the latest trends.

### Institutional 13F holdings (`/13f`)

Tracks 22 major funds including Berkshire Hathaway, Vanguard, BlackRock,
Bridgewater, Citadel, Point72, Tiger Global, Elliott, and ARK. Holdings are
sorted by value and quarter-over-quarter change is classified automatically.

### AI explanations

Powered by Google Gemini 2.5 Flash. Responses stream in real time and are
available in English and Simplified Chinese. Covers Federal Reserve data
trends, single-stock chart analysis, and news translation.

### Internationalisation

UI labels and AI responses support English (default) and Simplified Chinese
(中文), toggled via the language selector in the navbar.

### Daily email digest

Automated daily market report emailed to subscribers at UTC 00:00, summarising
key metrics across all tracked peer groups.

---

## Pages

| URL | Description |
|-----|-------------|
| `/` | Home - sector cards, valuation scatter, PEG map, cross-sector heatmap |
| `/sector/<name>` | Detail page for one peer group (PE, upside, PEG charts + data table) |
| `/history/<ticker>` | Single-ticker PE/PEG history, options wall, holders, news, AI |
| `/lookup` | Search any ticker or discover tickers by sector / industry |
| `/groups` | Add, remove, and manage peer groups (persisted to disk) |
| `/fed` | Federal Reserve balance sheet charts with AI trend explanations |
| `/13f` | Institutional 13F holdings from top hedge funds and asset managers |
| `/heatmap` | S&P 500 market heatmap by sector |
| `/markets` | Broad market overview (indices, commodities, crypto, ratios) |
| `/guide` | Help documentation and feature overview |
| `/videos` | Curated YouTube finance channels |
| `/refresh` | Clears the cache and triggers a background re-fetch |

---

## API endpoints

### Stock data

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cache-age` | GET | Cache metadata and age |
| `/api/ticker/<ticker>` | GET | Single ticker metrics |
| `/api/history/<ticker>` | GET | Historical PE / PEG / price data |
| `/api/history/<ticker>/explain` | POST | AI chart explanation (SSE stream) |
| `/api/financials/<ticker>` | GET | Income statement, balance sheet |
| `/api/discover` | GET | Sector / industry ticker discovery |
| `/api/forecast/<ticker>` | GET | 6-month price forecast (Prophet / ARIMA / linear) |

### Market data

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/markets` | GET | Broad market indices |
| `/api/fear-greed` | GET | CNN Fear & Greed index |
| `/api/put-call-ratio` | GET | Options sentiment |
| `/api/gold-ratios` | GET | Precious metals ratios |

### News & media

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/news/<ticker>` | GET | Recent news articles |
| `/api/news/translate` | POST | AI news translation |
| `/api/videos/<ticker>` | GET | Financial videos for ticker |
| `/api/videos/channel/<id>` | GET | Videos from a channel |

### Federal Reserve

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/fed` | GET | H.4.1 balance sheet data |
| `/api/fed/explain` | POST | AI Fed data explanation (SSE stream) |

### Institutional 13F

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/13f/<fund_slug>` | GET | Holdings for one fund |
| `/api/13f/ticker/<ticker>` | GET | Which funds own this stock |

---

## Caching

On startup the app warms an in-memory + on-disk cache by fetching all tickers
from Yahoo Finance in the background. The first page load shows a warming
screen with an auto-reload. Once the cache is populated, all pages are instant.

| Cache | TTL | File |
|-------|-----|------|
| Stock metrics | 8 hours | `cache/ticker_cache.json` |
| Fed balance sheet | 24 hours | `cache/fed_cache.json` |
| 13F holdings | 24 hours | `cache/sec13f_cache.json` |
| News | 5 minutes | in-memory only |

### Background tasks

| Task | Frequency | Description |
|------|-----------|-------------|
| Stock data cache warming | Every 8 hours | Fetches all peer group tickers from Yahoo |
| Fed data refresh | Every 24 hours | FRED series updates |
| 13F holdings refresh | Every 24 hours | SEC EDGAR fund holdings |
| Heatmap daily snapshot | Weekdays 16:30 ET | Stores heatmap state for historical view |
| Email broadcast | Daily 00:00 UTC | Sends daily market report emails |

All background tasks run as daemon threads (non-blocking shutdown). Use
`/refresh` to force an immediate re-fetch.

---

## Customising peer groups

Peer groups can be managed live via the `/groups` page - changes are saved to
`cache/peer_groups.json` and survive restarts.

To set the defaults, edit `PEER_GROUPS` in `ystocker/__init__.py`:

```python
PEER_GROUPS: dict[str, list[str]] = {
    "Tech":           ["MSFT", "AAPL", "GOOGL", "META", "NVDA"],
    "Semiconductors": ["NVDA", "AMD", "INTC", "QCOM", "TSM"],
    # Add more groups here
}
```

---

## Deploying to AWS with CloudFormation

`cloudformation.yaml` provisions a complete single-instance AWS stack:

- VPC with a public subnet and Internet Gateway
- EC2 instance (Amazon Linux 2023) with a persistent **Elastic IP**
- nginx reverse proxy (port 80) -> Gunicorn (port 8000)
- systemd service (`ystocker`) that starts on boot and auto-restarts
- IAM role with SSM Session Manager (optional SSH-free access)
- Security group: ports 22 (SSH), 80 (HTTP), 8000 (direct Gunicorn)

### Prerequisites

1. [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) installed and configured (`aws configure`)
2. An existing EC2 key pair in your target region - create one in the AWS Console under **EC2 -> Key Pairs** if needed

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `InstanceType` | `t3.small` | EC2 size (`t3.micro` is free-tier eligible) |
| `KeyName` | *(required)* | Name of your existing EC2 key pair |
| `AllowedSSHCidr` | `0.0.0.0/0` | Restrict SSH to your IP, e.g. `203.0.113.10/32` |
| `AppPort` | `8000` | Port Gunicorn listens on |
| `GitRepo` | *(empty)* | Optional HTTPS git URL to clone on first boot |

### Deploy via AWS CLI

```bash
aws cloudformation deploy \
  --template-file cloudformation.yaml \
  --stack-name ystocker \
  --parameter-overrides \
      KeyName=my-key-pair \
      AllowedSSHCidr=$(curl -s https://checkip.amazonaws.com)/32 \
      InstanceType=t3.small \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

After the stack finishes (~3 min), retrieve the outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name ystocker \
  --query "Stacks[0].Outputs" \
  --output table
```

### Deploy via AWS Console

1. Open **CloudFormation -> Stacks -> Create stack -> With new resources**
2. Choose **Upload a template file** and select `cloudformation.yaml`
3. Fill in the parameters (at minimum, set `KeyName`)
4. On the **Capabilities** page check **"I acknowledge that AWS CloudFormation might create IAM resources with custom names"**
5. Click **Create stack** and wait for `CREATE_COMPLETE`
6. Open the **Outputs** tab to find your `AppURL`

### Deploy the app code

If you left `GitRepo` blank (the default), upload your code after the stack is up:

```bash
ELASTIC_IP=<your-elastic-ip>

scp -i ~/.ssh/my-key-pair.pem -r ./ystocker ec2-user@$ELASTIC_IP:/tmp/

ssh -i ~/.ssh/my-key-pair.pem ec2-user@$ELASTIC_IP \
  'sudo cp -r /tmp/ystocker/* /opt/ystocker/ \
   && sudo pip install -r /opt/ystocker/requirements_stocker.txt \
   && sudo chown -R ystocker:ystocker /opt/ystocker \
   && sudo systemctl restart ystocker'
```

If you provided a `GitRepo` URL, the instance clones it automatically on first
boot and installs dependencies. No manual upload needed.

### Useful commands

```bash
# SSH into the instance
ssh -i ~/.ssh/my-key-pair.pem ec2-user@$ELASTIC_IP

# Tail app logs
ssh -i ~/.ssh/my-key-pair.pem ec2-user@$ELASTIC_IP \
  'sudo journalctl -u ystocker -f'

# Restart the app
ssh -i ~/.ssh/my-key-pair.pem ec2-user@$ELASTIC_IP \
  'sudo systemctl restart ystocker'

# Check nginx status
ssh -i ~/.ssh/my-key-pair.pem ec2-user@$ELASTIC_IP \
  'sudo systemctl status nginx'
```

### Tearing down

```bash
aws cloudformation delete-stack --stack-name ystocker --region us-east-1
```

> **Cost note:** An Elastic IP that is *not* associated with a running instance
> is billed by AWS. Deleting the stack releases the EIP automatically. A
> `t3.small` instance costs roughly $0.02/hr outside the free tier.

---

## Local production server

```bash
pip install gunicorn
gunicorn "ystocker:create_app()" --bind 0.0.0.0:8000
```

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `flask` | Web framework |
| `yfinance` | Stock data (prices, PE, PEG, analyst targets) from Yahoo Finance |
| `pandas` | Tabular data manipulation |
| `matplotlib` / `seaborn` | Server-side chart rendering |
| `requests` | HTTP client for FRED and SEC EDGAR |
| `google-genai` | Google Gemini API for AI explanations |
| `python-dotenv` | Load secrets from `.env` |
| `boto3` | AWS SSM Parameter Store (optional secret management) |
| `prophet` | Facebook/Meta time-series forecasting |
| `pmdarima` | AutoARIMA model selection |
| `statsmodels` | Statistical modelling |
| `numpy` | Numerical computing |
| `gunicorn` | Production WSGI server |

---

## License

[MIT](LICENSE)
