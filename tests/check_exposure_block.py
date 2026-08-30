"""Print a realistic constraint block. Diagnostic, not a test."""
from __future__ import annotations

from tests.test_exposure import resolver
from ystocker.exposure import PortfolioPolicy, Trade, assess, render_block
from ystocker.lookthrough import Position

positions = [Position("VOO", 40_000.0), Position("QQQ", 25_000.0),
             Position("BND", 20_000.0), Position("AAPL", 10_000.0),
             Position("GOOG", 3_000.0), Position("GOOGL", 2_000.0)]
policy = PortfolioPolicy(max_single_name_pct=8.0, max_issuer_pct=10.0,
                         cash=5_000.0,
                         holding_types={"AAPL": "core", "QQQ": "tactical"})
a = assess(positions, policy, resolver, trade=Trade("NVDA", 4_000.0),
           issuer_groups={"Alphabet": ["GOOG", "GOOGL"]})
print(render_block(a, top=6))
