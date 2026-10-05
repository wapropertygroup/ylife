# Security policy

This repository runs the Li family's web apps: stock.li-family.us,
trade-agents.com, pay.trade-agents.com and the other li-family.us subdomains
listed in the [README](README.md). If you have found a vulnerability in any of
them, thank you, and please tell us privately before anyone else.

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request about a
security problem.**

Report it privately through GitHub: on this repository's **Security** tab,
choose **Report a vulnerability**
(<https://github.com/wapropertygroup/ylife/security/advisories/new>). If that
option is not available to you, open an issue that asks for a private contact
and leaves out every detail of the problem.

A useful report includes:

- the URL, endpoint or file affected, and which app it belongs to;
- steps to reproduce, with a minimal proof of concept;
- what an attacker gains (data read, data changed, money spent, service
  degraded) and what they need first (being signed in, a VIP account, a share
  link);
- whether you have told anyone else.

## What to expect

This is a small family project with one maintainer, not a company with a
security team. As a guide:

- an acknowledgement within 7 days;
- an assessment and a severity within 14 days;
- for anything serious, a fix as soon as the problem is understood. A deploy
  takes about a minute, and every deploy is published as a release on this
  repository, so you can watch for the fix there;
- credit in the advisory and the release notes, if you would like it.

There is no bug bounty.

## Scope

In scope:

- the code in this repository and what it serves: the eight apps (yStocker,
  yPlanner, yPlanter, yHome, yTracker, yPay, yImage, yBG) on their domains;
- the deploy scripts and infrastructure templates in `deploy/`;
- the way yStocker runs TradingAgents (`ystocker/agents.py`). The framework
  itself lives in our fork,
  [wapropertygroup/TradingAgents](https://github.com/wapropertygroup/TradingAgents).

Out of scope:

- the apex `li-family.us`, which is GitHub Pages and not this server;
- third-party services the apps call (Stripe, Checkr, Google, Yahoo Finance,
  SEC EDGAR, FRED, Polymarket, Kalshi, Gemini, DeepSeek). Please report those
  to the vendor;
- denial of service, volumetric or rate-limit-exhaustion attacks, and anything
  found by sustained automated scanning (see the rules below);
- scanner output with no demonstrated impact, missing "best-practice" headers
  on their own, and self-XSS that needs the victim to paste code into their own
  browser console;
- social engineering of the maintainer or the family.

## Deliberate behaviour

Some things look like weaknesses and are design decisions. `CLAUDE.md` records
the reasoning for each.

- **A shared report link** (`/agents/shared/<token>`) can be read by anyone who
  holds it. It is a capability: 128 bits of randomness, a 30-day expiry,
  revocable by its owner, and the page masks the sharer's address. Forwarding
  the link re-shares the report.
- **The reading wall** over trade-agents.com's dashboards is soft. What it
  covers is public market data that the public JSON API also serves; the wall
  asks for an account and protects nothing.
- **The market-data APIs are public by design** (quotes, filings, odds, every
  dashboard's JSON). Crawlers that declare themselves are refused on `/api/*`;
  that is cost control, not access control.
- **No gate in yStocker reads a user table.** Every gate (credits, quotas,
  ownership of `/assets` positions and `/agents` runs) checks the signed-in
  session's email at the moment it is used. `ystocker-users` records only who
  has signed in, so the owner can be told about new sign-ups.
- **`/api/posts`** accepts writes from anyone holding its bearer token, and the
  feed it fills can only be read when signed in. With no token configured, the
  endpoint refuses every write.
- **`www.trade-agents.com` has no TLS certificate yet.** It is a known gap,
  recorded in `CLAUDE.md`.

## Rules for testing

Good-faith research is welcome. Please:

- use only accounts you own, and never read, change or delete anyone else's
  data. If you reach another person's data by accident, stop, keep no copy, and
  say so in your report;
- keep request volumes low. All eight apps share one small server, and they
  call vendors (Yahoo Finance, SEC EDGAR) whose rate limits we have to stay
  inside;
- spend no money but your own: an `/agents` run uses real API credit, and the
  pay pages take real payments;
- do not try to defeat the quotas, the daily build cap or the crawler guard at
  volume, and do not use the sharing features to send email or text messages to
  anyone who has not asked for them;
- give us reasonable time to fix a problem before you disclose it.

We will not pursue legal action against research that follows these rules.

## Supported versions

Only what is live is supported: the `main` branch, as deployed. Every deploy
is tagged `deploy-YYYY-MM-DD-HHMM` and published as a release, and fixes ship
as new deploys. No older version is maintained.

| Version | Supported |
|---|---|
| `main`, and the latest `deploy-*` release | Yes |
| Anything older | No |
