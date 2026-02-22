# yStocker

A Flask web application that fetches live stock data from Yahoo Finance and
displays PE ratios, PEG ratios, and analyst target prices across configurable
peer groups (Tech, Cloud/SaaS, Semiconductors, …).

---

## Project structure

```
ystocker/                   ← git repository root
├── run.py                  ← entry point — start the server from here
├── requirements.txt        ← Python dependencies
├── cloudformation.yaml     ← AWS deployment template
├── .gitignore
└── ystocker/               ← Python package (the app itself)
    ├── __init__.py         ← Flask app factory + PEER_GROUPS config
    ├── data.py             ← fetches stock metrics from Yahoo Finance
    ├── routes.py           ← URL routes / views
    ├── templates/
    │   ├── base.html       ← shared navbar + layout
    │   ├── index.html      ← home page (sector cards + cross-sector charts)
    │   ├── sector.html     ← per-sector detail page (charts + data table)
    │   ├── history.html    ← single-ticker PE/PEG history charts
    │   ├── lookup.html     ← ticker search + discover by sector
    │   ├── groups.html     ← manage peer groups
    │   └── warming.html    ← shown while cache is warming on startup
    └── static/
        └── css/
            └── style.css
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

### 3. Run the development server

```bash
python run.py
```

Open your browser at **http://127.0.0.1:5000**.

---

## Pages

| URL | Description |
|-----|-------------|
| `/` | Home — sector cards, valuation scatter, PEG map, heatmap table |
| `/sector/<name>` | Detail page for one peer group (PE, upside, PEG charts + data table) |
| `/history/<ticker>` | Single-ticker PE & PEG history (52-week charts) |
| `/lookup` | Search any ticker or discover tickers by sector/industry |
| `/groups` | Add, remove, and manage peer groups (changes are persisted to disk) |
| `/refresh` | Clears the cache and triggers a background re-fetch |

---

## Caching

On startup the app warms an in-memory + on-disk cache (`cache/ticker_cache.json`)
by fetching all tickers from Yahoo Finance in the background. The first page load
shows a warming screen with an auto-reload. Once the cache is populated, all pages
are instant. The cache expires after 24 hours and is refreshed automatically.

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
| `gunicorn` | Production WSGI server |
