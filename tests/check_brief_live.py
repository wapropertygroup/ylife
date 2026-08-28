"""One real end-to-end brief generation, printed for inspection.

Costs one Gemini call. Run with:  python tests/check_brief_live.py [en|zh] [warm]
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Reuse the matplotlib stubs and dotenv load from the collector check.
_boot = (Path(__file__).parent / "check_brief_collector.py").read_text()
exec(_boot.split("FAILURES: list[str] = []")[0], {"__file__": str(Path(__file__))})

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("urllib3", "ystocker.data", "matplotlib", "botocore", "boto3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from ystocker import routes  # noqa: E402

lang = sys.argv[1] if len(sys.argv) > 1 else "zh"
warm = "warm" in sys.argv[1:]
market = "cn" if "cn" in sys.argv[1:] else "us"

# Warming calls Flask views, which need an app context — the same reason the
# real pre-generator is handed the app. Build a minimal one rather than
# create_app(), which would start every background thread.
app = None
if warm:
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(routes.bp)

result = routes._generate_market_brief(lang, warm=warm, app=app, market=market)

print()
print("=" * 78)
print(f"market={market}  lang={lang}  warm={warm}  generated_at={result['generated_at']}")
print(f"used  ({len(result['sources_used'])}): {', '.join(result['sources_used'])}")
print(f"cold  ({len(result['sources_cold'])}): {', '.join(result['sources_cold'])}")
print(f"stale ({len(result['sources_stale'])}): {', '.join(result['sources_stale'])}")
brief_text = result["brief"]
print(f"length: {len(brief_text):,} chars  words(approx): {len(brief_text.split()):,}")
# Count separator rows, tolerating every alignment spelling Gemini uses:
# |---|, | --- |, |:---|, |:---:|.
import re as _re  # noqa: E402

SEP = _re.compile(r"^\s*\|[\s\-:|]+\|\s*$", _re.M)
tables = len(SEP.findall(brief_text))
headings = len(_re.findall(r"^##\s", brief_text, _re.M))
rows = len([l for l in brief_text.splitlines() if l.strip().startswith("|")]) - 2 * tables
print(f"markdown tables: {tables}   table rows: {rows}   h2 sections: {headings}")
unavail = len(_re.findall(r"^\s*\*[^*\n]*(?:unavailable|不可用)[^*\n]*\*\s*$",
                          brief_text, _re.M | _re.I))
print(f"one-line 'unavailable' sections: {unavail}")
print(f"placeholder tables (should be 0): {brief_text.count('DATA UNAVAILABLE')}")
print("=" * 78)
print(brief_text)
