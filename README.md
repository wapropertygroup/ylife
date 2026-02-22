# yStocker

A Flask web application for stock research and portfolio analysis. Fetches live
data from Yahoo Finance, tracks Federal Reserve balance sheet trends, monitors
institutional 13F holdings, and provides AI-powered explanations — all across
configurable peer groups.

---

## Project structure

```
ystocker/                   ← git repository root
├── run.py                  ← entry point — start the server from here
├── requirements.txt        ← Python dependencies
├── cloudformation.yaml     ← AWS deployment template
├── deploy.sh               ← deployment helper script
├── .gitignore
├── cache/                  ← persistent on-disk cache (auto-created)
│   ├── ticker_cache.json   ← stock metrics (8h TTL)
│   ├── peer_groups.json    ← user-managed peer groups
│   ├── fed_cache.json      ← Federal Reserve data (24h TTL)
│   └── sec13f_cache.json   ← SEC 13F holdings (24h TTL)
└── ystocker/               ← Python package (the app itself)
    ├── __init__.py         ← Flask app factory + PEER_GROUPS config
    ├── data.py             ← fetches stock metrics from Yahoo Finance
    ├── routes.py           ← URL routes / views + JSON API endpoints
    ├── fed.py              ← Federal Reserve FRED data fetching
    ├── sec13f.py           ← SEC EDGAR 13F holdings fetching
    ├── charts.py           ← matplotlib/seaborn chart generation
    ├── templates/
    │   ├── base.html       ← shared navbar + layout
    │   ├── index.html      ← home page (sector cards + cross-sector charts)
    │   ├── sector.html     ← per-sector detail page (charts + data table)
    │   ├── history.html    ← single-ticker PE/PEG history + options wall
    │   ├── lookup.html     ← ticker search + discover by sector
    │   ├── groups.html     ← manage peer groups
    │   ├── fed.html        ← Federal Reserve balance sheet charts
    │   ├── thirteenf.html  ← institutional 13F holdings
    │   └── warming.html    ← shown while cache is warming on startup
    └── static/
        ├── css/style.css
        └── i18n.js         ← English / Simplified Chinese translations
```

---

## Quick start

### 1. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
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
python run.py
```

Open your browser at **http://127.0.0.1:5000**.

---

## Pages

| URL | Description |
|-----|-------------|
| `/` | Home — sector cards, valuation scatter, PEG map, cross-sector heatmap |
| `/sector/<name>` | Detail page for one peer group (PE, upside, PEG charts + data table) |
| `/history/<ticker>` | Single-ticker PE/PEG history, options wall, institutional holders, news |
| `/lookup` | Search any ticker or discover tickers by sector / industry |
| `/groups` | Add, remove, and manage peer groups (changes are persisted to disk) |
| `/fed` | Federal Reserve balance sheet charts with AI trend explanations |
| `/13f` | Institutional 13F holdings from top hedge funds and asset managers |
| `/refresh` | Clears the cache and triggers a background re-fetch |

---

## Features

### Peer group valuation dashboard

The home page and per-sector pages display forward PE, TTM PE, PEG ratios,
analyst price targets, upside %, EPS growth, and market cap for every ticker in
each peer group. Charts include bar comparisons, a valuation scatter plot
(Forward PE vs analyst upside), and a color-coded heatmap.

### Single-ticker analysis (`/history/<ticker>`)

- Historical PE / PEG / price charts (configurable period: 1 month – 5 years)
- Options wall — aggregated call/put open interest across all expirations to
  visualise support and resistance levels
- Institutional holders ranked by portfolio weight, value, and change
- AI-powered chart explanation (streams via Server-Sent Events, English and
  Chinese supported)
- Recent news with importance scoring

### Federal Reserve dashboard (`/fed`)

Weekly H.4.1 data pulled directly from FRED (no API key required). Charts
cover Total Assets, Treasury Holdings, MBS, Reserve Balances, ON RRP, and
Currency in Circulation. AI explanations summarise the latest trends.

### Institutional 13F holdings (`/13f`)

Tracks 22 major funds including Berkshire Hathaway, Vanguard, BlackRock,
Bridgewater, Citadel, Point72, Tiger Global, Elliott, and ARK. Holdings are
sorted by value and quarter-over-quarter change is classified automatically.

### AI explanations

Powered by Google Gemini 2.5 Flash. Responses stream in real time and are
available in English and Simplified Chinese. Covers both Federal Reserve data
trends and single-stock chart analysis.

### Internationalisation

UI labels and AI responses support English (default) and Simplified Chinese
(中文), toggled via the language selector in the navbar.

---

## Caching

On startup the app warms an in-memory + on-disk cache (`cache/ticker_cache.json`)
by fetching all tickers from Yahoo Finance in the background. The first page
load shows a warming screen with an auto-reload. Once the cache is populated,
all pages are instant.

| Cache | TTL | File |
|-------|-----|------|
| Stock metrics | 8 hours | `cache/ticker_cache.json` |
| Fed balance sheet | 24 hours | `cache/fed_cache.json` |
| 13F holdings | 24 hours | `cache/sec13f_cache.json` |
| News | 5 minutes | in-memory only |

The cache is also refreshed automatically every 8 hours in a background thread.
Use `/refresh` to force an immediate re-fetch.

---

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/cache-age` | Cache metadata and age |
| `GET /api/ticker/<ticker>` | Single ticker metrics (JSON) |
| `GET /api/history/<ticker>` | Historical PE / PEG / price data (JSON) |
| `GET /api/history/<ticker>/explain` | AI chart explanation (SSE stream) |
| `GET /api/news/<ticker>` | Recent news articles (JSON) |
| `GET /api/discover` | Sector / industry ticker discovery (JSON) |
| `GET /api/fed` | Federal Reserve H.4.1 data (JSON) |
| `GET /api/fed/explain` | AI Fed data explanation (SSE stream) |
| `GET /api/13f/<fund_slug>` | Institutional holdings for one fund (JSON) |

---

## Customising peer groups

Peer groups can be managed live via the `/groups` page — changes are saved to
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
- nginx reverse proxy (port 80) → Gunicorn (port 8000)
- systemd service (`ystocker`) that starts on boot and auto-restarts
- IAM role with SSM Session Manager (optional SSH-free access)
- Security group: ports 22 (SSH), 80 (HTTP), 8000 (direct Gunicorn)

### Prerequisites

1. [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) installed and configured (`aws configure`)
2. An existing EC2 key pair in your target region — create one in the AWS Console under **EC2 → Key Pairs** if needed

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `InstanceType` | `t3.small` | EC2 size (`t3.micro` is free-tier eligible) |
| `KeyName` | *(required)* | Name of your existing EC2 key pair |
| `AllowedSSHCidr` | `0.0.0.0/0` | Restrict SSH to your IP, e.g. `203.0.113.10/32` |
| `AppPort` | `8000` | Port Gunicorn listens on |
| `GitRepo` | *(empty)* | Optional HTTPS git URL to clone on first boot |

### Option A — deploy via AWS CLI

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

The `AppURL` output gives you the public URL, e.g. `http://54.x.x.x/`.

### Option B — deploy via AWS Console

1. Open **CloudFormation → Stacks → Create stack → With new resources**
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
   && sudo pip install -r /opt/ystocker/requirements.txt \
   && sudo chown -R ystocker:ystocker /opt/ystocker \
   && sudo systemctl restart ystocker'
```

If you provided a `GitRepo` URL, the instance clones it automatically on first
boot and installs `requirements.txt`. No manual upload needed.

### Useful commands after deployment

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

### Updating the app

```bash
scp -i ~/.ssh/my-key-pair.pem -r ./ystocker ec2-user@$ELASTIC_IP:/tmp/
ssh -i ~/.ssh/my-key-pair.pem ec2-user@$ELASTIC_IP \
  'sudo cp -r /tmp/ystocker/* /opt/ystocker/ \
   && sudo chown -R ystocker:ystocker /opt/ystocker \
   && sudo systemctl restart ystocker'
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
| `gunicorn` | Production WSGI server |
