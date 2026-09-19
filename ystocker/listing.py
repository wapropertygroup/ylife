"""
ystocker.listing
~~~~~~~~~~~~~~~~
Reconciling what a share is *quoted* in with what its filer *reports* in.

Pure — no network, no clock — for the reason :mod:`ystocker.dcf` and
:mod:`ystocker.lookthrough` are: the arithmetic below is the only part that can
be wrong, and it is only cheaply provable if proving it needs no I/O.

Why this exists
---------------
An ADR is priced in one currency, on one share basis, while the company behind
it files in another currency on another basis. Yahoo hands both back from the
same ``Ticker`` and reconciles neither. Measured on TSM on 2026-09-19::

    price                        434.67   USD per ADR
    Diluted EPS (statement, TTM) 431.35   TWD per ADR
    Ordinary Shares Number      25.93e9   ordinary shares
    info.sharesOutstanding       5.19e9   ADRs  (= ordinary / 5, exactly)
    info.trailingEps              13.39   USD per ADR

So ``close / eps`` is **1.01**, and ``/dca/TSM`` reported a P/E of 1.01 against a
true 32.46. That is not a rounding error, it is a factor of 32 — and it runs in
the direction that makes a company look extraordinarily cheap, on a page whose
entire output is a *cheapness* score driving a contribution multiplier. The
matching P/FCF read 11.4 where the truth is ~73.

Two independent corrections, and they are not the same correction
-----------------------------------------------------------------
* **Currency.** The statements are in the filer's currency and the price is in
  the listing's. One FX multiplier fixes every money field.
* **Share basis.** ``Ordinary Shares Number`` counts the *company's* shares while
  the price is per *depositary receipt*. On TSM those differ by exactly 5.

Fixing only the first leaves market cap wrong by the ADR ratio, which is worse
than leaving both wrong, because P/E would then look right and P/FCF would not —
and a reader has no way to tell which columns were repaired.

Every factor is a ratio, so this is unit-invariant
--------------------------------------------------
``reconstruct()`` produces nothing but ratios of a price to a statement figure,
which means converting the statements into the price's currency and converting
the price into the statements' currency give *identical* multiples. This module
does the former, because that also leaves the stored per-share DCF fair value in
the currency the reader sees quoted. Converting the price instead would produce
the same percentiles and a fair-value-per-share in TWD sitting next to a USD
quote.

The EPS basis is measured, not assumed
--------------------------------------
Yahoo's ``Diluted EPS`` row is per-ADR for TSM — it is 5.06x the figure implied
by ``Net Income / Ordinary Shares Number``. Assuming that holds for every ADR is
exactly the kind of guess this codebase keeps getting punished for, so
:func:`eps_scale` *checks* it: the converted statement EPS is compared against
``info.trailingEps``, which is independently published on the quoted basis in the
quoted currency. A correction is accepted only when it lands near 1.0 or near the
share ratio — the two answers that mean something — and anything else is reported
as unknown rather than split the difference.

Nothing here guesses an ADR ratio from a table of known ones. It is derived from
the two share counts the payload already carries, which is why it needs no
maintenance as ratios change.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "MONEY_FIELDS", "SHARE_FIELDS", "PER_SHARE_FIELDS",
    "MAX_SHARE_RATIO", "SHARE_RATIO_TOLERANCE", "EPS_MATCH_TOLERANCE",
    "Basis", "detect", "fx_pair", "share_ratio", "eps_scale",
    "rate_at", "convert",
]

#: Vintage fields carried in the filer's reporting currency. Absolute totals and
#: per-share amounts alike — both are scaled by the same FX multiplier.
MONEY_FIELDS = frozenset({
    "revenue", "net_income", "eps", "ebitda", "debt", "cash",
    "tangible_book", "fcf", "ffo",
})

#: Fields counting shares. These carry no currency at all and need the *other*
#: correction, which is why they are a separate set rather than an exclusion.
SHARE_FIELDS = frozenset({"shares"})

#: Money fields quoted *per share* rather than as a total. These need the share
#: basis applied as well as the currency, but only when :func:`eps_scale` has
#: measured that the filer quotes them on the ordinary basis.
PER_SHARE_FIELDS = frozenset({"eps"})

#: Beyond this, the two share counts are not an ADR ratio — they are a bad read.
#: Real ratios run from about 1:100 to 100:1; a factor of 1e4 means one of the
#: numbers is in the wrong unit and the right answer is to refuse.
MAX_SHARE_RATIO = 1000.0

#: Inside this band the two counts are the same number and the difference is
#: treasury stock or a basic/diluted mismatch, not a depositary ratio.
SHARE_RATIO_TOLERANCE = 0.05

#: How close a converted EPS has to sit to ``trailingEps`` to call the basis
#: settled. Wide because the two are struck at different moments: the statement
#: EPS is a TTM through the last period end and ``trailingEps`` is Yahoo's own,
#: which can include a quarter this reconstruction has not folded in yet.
EPS_MATCH_TOLERANCE = 0.25


def _fin(value: Any) -> bool:
    """Finite and numeric, excluding ``bool`` — which is an ``int`` in Python and
    would otherwise pass as a share count of 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _pos(value: Any) -> bool:
    return _fin(value) and float(value) > 0


@dataclass(frozen=True)
class Basis:
    """How a listing's quote relates to its filer's statements.

    ``aligned`` is the overwhelmingly common case — a US company filing in USD
    with one share per share — and it short-circuits every caller, so the ~59 of
    60 tracked tickers that need nothing pay nothing.

    ``usable`` is the question a caller actually asks. It is False only when the
    currencies differ *and* no rate could be found, which is the one state where
    publishing the multiples would mean publishing the 32x error above.
    """

    statement_currency: Optional[str] = None
    price_currency: Optional[str] = None
    share_ratio: float = 1.0
    eps_scale: float = 1.0
    fx_source: Optional[str] = None          # "series" | "spot" | None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def aligned(self) -> bool:
        """True when the quote and the statements already agree on both axes."""
        return (not self.needs_fx
                and self.share_ratio == 1.0
                and self.eps_scale == 1.0)

    @property
    def needs_fx(self) -> bool:
        return bool(self.statement_currency
                    and self.price_currency
                    and self.statement_currency != self.price_currency)

    @property
    def usable(self) -> bool:
        return (not self.needs_fx) or self.fx_source is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "statement_currency": self.statement_currency,
            "price_currency": self.price_currency,
            "share_ratio": round(self.share_ratio, 6),
            "eps_scale": round(self.eps_scale, 6),
            "fx_source": self.fx_source,
            "notes": list(self.notes),
        }


def fx_pair(statement_currency: Optional[str],
            price_currency: Optional[str]) -> Optional[str]:
    """Yahoo's symbol for "one unit of the statement currency, priced in the
    listing's currency", or ``None`` when no conversion is needed.

    ``TWDUSD=X`` quotes TWD->USD directly, so the result is the multiplier
    wanted. :func:`ystocker.data.usd_rate` picks the same direction, and for the
    same reason: the inverse pair (``TWD=X`` is USD/TWD) needs a division and
    reads backwards at the call site.
    """
    src = (statement_currency or "").strip().upper()
    dst = (price_currency or "").strip().upper()
    if not src or not dst or src == dst:
        return None
    return f"{src}{dst}=X"


def share_ratio(statement_shares: Any, quoted_shares: Any) -> tuple[float, Optional[str]]:
    """Filer shares per quoted share, with the reason when it is not applied.

    Returns ``(ratio, note)``. A ratio of 1.0 means "no correction", which is
    both the answer for an ordinary listing and the safe answer whenever the
    inputs cannot support a better one.

    The ratio is derived from *today's* pair and then applied to every historical
    vintage. That is right rather than convenient: a depositary ratio is fixed by
    the deposit agreement, so it does not drift with buybacks the way either
    count does on its own. It does change at a ratio-change corporate action,
    which is rare and which no Yahoo field announces — noted here because it is
    the one input that could go quietly stale.
    """
    if not _pos(statement_shares) or not _pos(quoted_shares):
        return 1.0, "share_counts_unavailable"
    ratio = float(statement_shares) / float(quoted_shares)
    if not math.isfinite(ratio) or ratio <= 0:
        return 1.0, "share_ratio_unreadable"
    if abs(ratio - 1.0) <= SHARE_RATIO_TOLERANCE:
        # Treasury stock or basic-vs-diluted. Not a depositary ratio, and
        # "correcting" by 2% would add noise to every cap in the history.
        return 1.0, None
    if ratio > MAX_SHARE_RATIO or ratio < 1.0 / MAX_SHARE_RATIO:
        return 1.0, "share_ratio_implausible"
    return ratio, None


def eps_scale(statement_eps: Any, reference_eps: Any, *,
              rate: Optional[float], ratio: float) -> tuple[float, Optional[str]]:
    """Multiplier putting the filer's EPS row onto the quoted share basis.

    *statement_eps* is the latest reconstructed TTM EPS in the filer's currency;
    *reference_eps* is ``info.trailingEps``, which Yahoo publishes on the quoted
    basis in the quoted currency; *rate* converts the first into the second's
    currency.

    Measured on TSM, the statement row is already per-ADR, so the answer is 1.0.
    That is **not** assumed for every issuer: the two are compared, and a
    correction of ``ratio`` is accepted only when it is what actually reconciles
    them. Anything else returns 1.0 with a note, because a half-applied share
    basis is the failure mode this whole module exists to prevent and inventing a
    third scaling to make two numbers meet would be exactly that.
    """
    if not _pos(statement_eps) or not _pos(reference_eps) or not _pos(rate):
        return 1.0, "eps_basis_unverified"
    converted = float(statement_eps) * float(rate)
    as_is = abs(converted / float(reference_eps) - 1.0)
    if as_is <= EPS_MATCH_TOLERANCE:
        return 1.0, None
    if ratio > 1.0:
        scaled = abs(converted * ratio / float(reference_eps) - 1.0)
        if scaled <= EPS_MATCH_TOLERANCE and scaled < as_is:
            return ratio, "eps_rebased_to_quoted_shares"
    return 1.0, "eps_basis_unexplained"


def rate_at(series: Sequence[tuple[str, float]], stamp: str, *,
            fallback: Optional[float] = None) -> Optional[float]:
    """The rate in force on *stamp*: the last observation on or before it.

    A weekly FX series does not line up with a weekly price series — different
    exchanges, different holidays — so this is an as-of lookup rather than a
    dictionary hit, and a missing week resolves to the most recent known rate
    instead of dropping the point.

    Before the series starts, the **earliest** rate is used rather than
    *fallback*. A 2021 statement converted at today's rate is wrong by the whole
    five-year drift; converted at the first rate on file it is wrong by however
    far that week sits from it, which is smaller by construction.
    """
    if series:
        stamps = [row[0] for row in series]
        index = bisect_right(stamps, stamp)
        if index:
            return float(series[index - 1][1])
        return float(series[0][1])
    return fallback


def detect(info: Mapping[str, Any], *,
           statement_shares: Any = None,
           statement_eps: Any = None,
           rate: Optional[float] = None,
           fx_source: Optional[str] = None) -> Basis:
    """Work out both corrections from one ``info`` dict and two statement reads.

    *rate* and *fx_source* are injected rather than fetched, which is what keeps
    this module pure and its tests free of a network. The caller decides whether
    it got the rate from a history series or a spot quote; this only records
    which.
    """
    statement_currency = (info.get("financialCurrency") or "").strip().upper() or None
    price_currency = (info.get("currency") or "").strip().upper() or None

    notes: list[str] = []
    ratio, note = share_ratio(statement_shares, info.get("sharesOutstanding"))
    if note:
        notes.append(note)

    needs_fx = bool(statement_currency and price_currency
                    and statement_currency != price_currency)

    # A US company filing in the currency it trades in, one share per share, is
    # the overwhelmingly common case and there is nothing to correct. Returning
    # early keeps it on exactly the path it was on before this module existed —
    # and, just as importantly, keeps the EPS cross-check from running where it
    # has no correction to choose between, since it would then spend its one
    # tolerance budget flagging ordinary TTM-vintage drift as a basis mystery.
    if not needs_fx and ratio == 1.0:
        return Basis(statement_currency=statement_currency,
                     price_currency=price_currency,
                     notes=tuple(notes))

    scale, note = eps_scale(statement_eps, info.get("trailingEps"),
                            rate=rate, ratio=ratio)
    if note:
        notes.append(note)

    return Basis(
        statement_currency=statement_currency,
        price_currency=price_currency,
        share_ratio=ratio,
        eps_scale=scale,
        fx_source=fx_source if needs_fx else None,
        notes=tuple(notes),
    )


def convert(fields: Mapping[str, Any], *, rate: Optional[float],
            ratio: float = 1.0, scale: float = 1.0) -> dict[str, Any]:
    """One vintage's fields, restated in the quoted currency and share basis.

    *rate* multiplies every money field; *ratio* divides every share count;
    *scale* additionally multiplies the per-share money fields. Unknown keys pass
    through untouched, so this cannot silently drop a field a caller added to the
    ``Vintage`` slots and forgot to classify here — it would simply go
    unconverted, which a summation test catches, rather than vanishing.

    A *rate* of ``None`` leaves money alone. That is only correct when the
    currencies already match, and the caller is responsible for not publishing a
    reconstruction where they do not — see :attr:`Basis.usable`.
    """
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if not _fin(value):
            out[key] = value
            continue
        number = float(value)
        if key in MONEY_FIELDS:
            if rate is not None:
                number *= float(rate)
            if key in PER_SHARE_FIELDS and scale != 1.0:
                number *= float(scale)
        elif key in SHARE_FIELDS and ratio and ratio != 1.0:
            number /= float(ratio)
        out[key] = number
    return out
