"""Wrap chart colour literals in `CT.c(...)` so they follow the theme.

A canvas takes no CSS, so the ~490 colour literals in the dashboards' Chart.js
configs are the one part of the UI Tailwind's `dark:` variant cannot reach.
`CT.c()` (defined in base.html) maps a dark-mode colour to its light-mode
counterpart and returns anything it does not recognise unchanged.

Scope is deliberately narrow on two axes:

  * Only inside <script> blocks. The same hex values also appear in <style>
    blocks and inline `style=` attributes as raw, unquoted CSS, where a JS call
    would be a syntax error rather than a colour.
  * Only *single-quoted* literals. That is the form Chart.js configs use here;
    matching bare `#1e293b` would hit the raw-CSS occurrences above.

The literal set is kept in step with base.html's MAP by hand. A value missing
from that table is still safe to wrap -- CT.c() is the identity for unknown
colours -- so drift degrades to "no theming" rather than to a wrong colour.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Must mirror the keys of window.CT's MAP in base.html.
LITERALS = [
    # chrome
    "#0f172a", "#1e293b", "#334155", "#475569", "#64748b", "#94a3b8",
    "#cbd5e1", "#e2e8f0",
    "rgba(51,65,85,0.3)", "rgba(148,163,184,.55)",
    # data series
    "#34d399", "#f87171", "#6366f1", "#a5b4fc", "#a78bfa", "#38bdf8",
    "#fbbf24", "#fb923c",
    # semantic axis hues
    "rgba(248,113,113,0.5)", "rgba(248,113,113,0.6)", "rgba(248,113,113,0.7)",
    "rgba(56,189,248,0.6)", "rgba(52,211,153,0.5)", "rgba(52,211,153,0.6)",
    "rgba(251,191,36,0.6)", "rgba(251,191,36,0.7)", "rgba(245,158,11,0.6)",
    "rgba(167,139,250,0.6)", "rgba(167,139,250,0.7)", "rgba(251,146,60,0.6)",
]

SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
QUOTED = re.compile("|".join(re.escape(f"'{lit}'") for lit in LITERALS))

# base.html must never be processed: it *defines* window.CT, so its MAP table is
# a wall of these very literals in key position. Wrapping them produced
# `CT.c('#0f172a'): '#ffffff'` -- a syntax error -- and the two Chart.defaults
# assignments became `CT.c(...)` calls evaluated inside the IIFE that has not
# returned yet, so CT was still undefined. It also has no chart of its own.
SKIP = {"base.html"}


def wrap_scripts(text: str) -> tuple[str, int]:
    count = 0

    def in_block(block: re.Match[str]) -> str:
        nonlocal count

        def repl(m: re.Match[str]) -> str:
            nonlocal count
            start = m.start()
            # Idempotency: skip a literal already sitting inside CT.c(...).
            if block.group(0)[max(0, start - 5):start] == "CT.c(":
                return m.group(0)
            count += 1
            return f"CT.c({m.group(0)})"

        return QUOTED.sub(repl, block.group(0))

    return SCRIPT_BLOCK.sub(in_block, text), count


def main(paths: list[str]) -> int:
    total = 0
    for p in paths:
        path = Path(p)
        if path.name in SKIP:
            print(f"    -  {path} (skipped: defines CT)")
            continue
        original = path.read_text(encoding="utf-8")
        converted, n = wrap_scripts(original)
        if n:
            path.write_text(converted, encoding="utf-8")
        total += n
        print(f"{n:5d}  {path}")
    print(f"\n{total} literals wrapped across {len(paths)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
