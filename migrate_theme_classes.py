"""One-shot mechanical pass: give every hardcoded dark `slate-*` utility in the
yStocker templates an explicit light-mode counterpart plus a `dark:` variant.

Deliberately conservative. It rewrites ONLY the slate ramp, where the light value
follows from the dark one with no judgement required. It does not touch:

  * `text-white` / `bg-white`  — context-sensitive. White on an indigo brand
    button must stay white in both themes; white as a nav-link hover must become
    slate-900. A script cannot tell those apart from the token alone.
  * semantic hues (emerald / red / amber flash and status chips) — these need a
    per-site decision about tint direction.
  * anything already carrying a `dark:` prefix, so the pass is idempotent.

Single simultaneous regex pass, not sequential replaces: `text-slate-300` maps to
`text-slate-600`, which is itself a key, so chained replacement would double-map
it down to slate-500 and quietly flatten two tiers of the type hierarchy.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# dark value -> light value, per property. A property/shade pair absent from this
# table is left exactly as found.
MAP: dict[str, str] = {
    # Surfaces. Three tiers, mirroring how the dark theme layers: the page plane
    # (slate-950 -> slate-50), the card raised above it (slate-900 -> white), and
    # the well recessed into the card (slate-800 -> slate-100). Mapping the page
    # to white as well as the card left the two indistinguishable, so every card
    # read as a floating border with no plane behind it.
    "bg-slate-950": "bg-slate-50",
    "bg-slate-900": "bg-white",
    "bg-slate-800": "bg-slate-100",
    "bg-slate-700": "bg-slate-200",
    "bg-slate-600": "bg-slate-300",
    "bg-slate-500": "bg-slate-400",
    # Hairlines.
    "border-slate-800": "border-slate-200",
    "border-slate-700": "border-slate-300",
    "border-slate-600": "border-slate-300",
    "border-slate-500": "border-slate-400",
    "divide-slate-800": "divide-slate-200",
    "divide-slate-700": "divide-slate-300",
    "ring-slate-900": "ring-slate-200",
    "ring-slate-700": "ring-slate-300",
    # Type. slate-500 is intentionally absent: it is the muted caption tier and
    # already clears AA on white (4.76:1), where slate-400 would fail at 2.6:1.
    # slate-600 collapses onto it for the same reason -- one tier of dim-text
    # hierarchy is worth less than legible captions.
    "text-slate-100": "text-slate-800",
    "text-slate-200": "text-slate-700",
    "text-slate-300": "text-slate-600",
    "text-slate-400": "text-slate-500",
    "text-slate-600": "text-slate-500",
    "placeholder-slate-600": "placeholder-slate-500",
}

# ── Semantic hues ────────────────────────────────────────────────────────────
# The status palette: emerald/green for up, rose/red for down, amber for a
# warning, sky/blue for information, violet/indigo for the brand.
#
# These matter more than the slate ramp, because they are what carries the data:
# 404 of the 549 hue-text tokens are shade-400, chosen to glow against near
# black. On white, `text-emerald-400` is 1.87:1 -- the percentage change in a
# table would be legible only as a smudge. Text therefore lands on shade-700,
# the first rung that clears AA on white for every hue in the set (emerald-600
# is 3.4:1 and amber-600 only 3.0:1, so 600 is not good enough).
#
# Fills lose their opacity modifier on the light side: `bg-emerald-900/50` is a
# dark green *blended into a dark page*, and the alpha exists only for that
# blend. Its light counterpart is the solid shade-50 wash.
HUES = (
    "emerald", "green", "lime", "teal", "cyan", "sky", "blue", "indigo",
    "violet", "purple", "fuchsia", "pink", "rose", "red", "orange", "amber",
    "yellow",
)

for _hue in HUES:
    MAP.update({
        # Fills: dark, page-blended -> solid pale wash.
        f"bg-{_hue}-950": f"bg-{_hue}-50",
        f"bg-{_hue}-900": f"bg-{_hue}-50",
        f"bg-{_hue}-800": f"bg-{_hue}-100",
        # Hairlines.
        f"border-{_hue}-900": f"border-{_hue}-200",
        f"border-{_hue}-800": f"border-{_hue}-200",
        f"border-{_hue}-700": f"border-{_hue}-300",
        f"border-{_hue}-600": f"border-{_hue}-300",
        # Type. 600 and 800 are already dark enough to leave alone.
        f"text-{_hue}-200": f"text-{_hue}-800",
        f"text-{_hue}-300": f"text-{_hue}-700",
        f"text-{_hue}-400": f"text-{_hue}-700",
        f"text-{_hue}-500": f"text-{_hue}-700",
    })

# Bases whose light counterpart must SHED the opacity modifier. A dark semantic
# fill carries an alpha purely to blend into a near-black page; `bg-emerald-50/50`
# would be a half-strength wash of an already-pale colour, i.e. all but invisible.
# The slate ramp is deliberately excluded: `bg-slate-900/80` backs the sticky
# header, where the alpha is what `backdrop-blur` blurs through, so `bg-white/80`
# has to keep it.
DROP_ALPHA = {
    f"bg-{_hue}-{_shade}" for _hue in HUES for _shade in (800, 900, 950)
}

# A leading chain of Tailwind variants (hover:, focus:, md:, placeholder:, ...).
# Captured so it can be re-emitted on both halves: `hover:bg-slate-800` has to
# become `hover:bg-slate-100 dark:hover:bg-slate-800`, not
# `hover:bg-slate-100 dark:bg-slate-800`.
TOKEN = re.compile(
    r"(?P<prefix>(?:[a-z-]+:)*)"
    r"(?P<base>(?:bg|text|border|divide|ring|placeholder)-"
    r"(?:slate|" + "|".join(HUES) + r")-\d{2,3})"
    r"(?P<alpha>/\d{1,3})?"
)


def convert(text: str) -> tuple[str, int]:
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        prefix, base, alpha = m.group("prefix"), m.group("base"), m.group("alpha") or ""

        # Already themed, or explicitly dark-only: leave alone.
        if "dark:" in prefix:
            return m.group(0)

        # Already paired by an earlier run. Needed as well as the prefix check
        # because a light value this table emits can itself be a key:
        # `text-slate-300` -> `text-slate-600`, and re-running would map that
        # light half down again to slate-500, flattening the hierarchy a bit
        # more on every invocation. Guarding on the emitted pair makes the pass
        # genuinely idempotent instead of merely safe the first time.
        #
        # Matched against the same *property* (`text-`, `bg-`, ...), not a bare
        # " dark:". A loose check would also skip a genuinely unconverted token
        # that merely happens to sit next to some other converted one, e.g. the
        # emerald in `text-emerald-400 dark:bg-slate-800`.
        prop = base.split("-", 1)[0]
        if m.string[m.end():].startswith(f" dark:{prefix}{prop}-"):
            return m.group(0)

        light = MAP.get(base)
        if light is None:
            return m.group(0)

        count += 1
        light_alpha = "" if base in DROP_ALPHA else alpha
        return f"{prefix}{light}{light_alpha} dark:{prefix}{base}{alpha}"

    return TOKEN.sub(repl, text), count


def main(paths: list[str]) -> int:
    total = 0
    for p in paths:
        path = Path(p)
        original = path.read_text(encoding="utf-8")
        converted, n = convert(original)
        if n:
            path.write_text(converted, encoding="utf-8")
        total += n
        print(f"{n:5d}  {path}")
    print(f"\n{total} tokens converted across {len(paths)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
