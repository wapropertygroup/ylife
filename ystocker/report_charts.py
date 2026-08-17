"""
ystocker.report_charts
~~~~~~~~~~~~~~~~~~~~~~
Charts for the agent PDF report.

The data source is the OHLCV CSV the run itself downloaded into the
TradingAgents cache, not a fresh fetch. Two reasons: the PDF is built inside a
web request, where a Yahoo call would add seconds of latency and a failure mode
for something that is only illustrative; and more importantly the chart then
shows *exactly* the prices the agents reasoned about, so the picture and the
prose cannot disagree.

Follows the ``charts.py`` pattern already established here -- matplotlib with
the Agg backend, figures closed after rendering -- but styles itself through
``plt.rc_context`` rather than touching global rcParams, because charts.py sets
a seaborn theme at import for the rest of the site and a PDF must not change
how the web charts look.
"""
from __future__ import annotations

import glob
import io
import logging
import os
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

# Light theme: these land on a white PDF page, not the site's dark UI.
_INK = "#1e293b"
_MUTED = "#64748b"
_GRID = "#e2e8f0"
_PRICE = "#2563eb"
_SMA50 = "#f59e0b"
_SMA200 = "#7c3aed"
_UP = "#16a34a"
_DOWN = "#dc2626"

_RC = {
    "figure.dpi": 130,
    "savefig.dpi": 130,
    "font.size": 7.5,
    "axes.edgecolor": _GRID,
    "axes.labelcolor": _MUTED,
    "axes.titlecolor": _INK,
    "axes.titlesize": 8.5,
    "axes.titleweight": "bold",
    "xtick.color": _MUTED,
    "ytick.color": _MUTED,
    "grid.color": _GRID,
    "grid.linewidth": 0.6,
    "axes.grid": True,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "legend.frameon": False,
    "legend.fontsize": 7,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
}


def _cache_dir() -> str:
    """Where TradingAgents keeps its OHLCV cache.

    Resolved the same way ``tradingagents.default_config`` does, so the two
    cannot point at different directories on the same host.
    """
    env = os.environ.get("TRADINGAGENTS_CACHE_DIR", "").strip()
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".tradingagents", "cache")


def find_ohlcv(ticker: str) -> Optional[str]:
    """Newest cached OHLCV CSV for a ticker, or None."""
    safe = re.sub(r"[^A-Za-z0-9.=\-^]", "", (ticker or "").upper())
    if not safe:
        return None
    pattern = os.path.join(_cache_dir(), f"{safe}-YFin-data-*.csv")
    hits = [p for p in glob.glob(pattern) if os.path.getsize(p) > 0]
    if not hits:
        return None
    return max(hits, key=os.path.getmtime)


def _rsi(close, period: int = 14):
    """Wilder's RSI, the definition stockstats uses, so the chart agrees with
    the value the market analyst quotes in the text."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def load_frame(ticker: str, months: int = 24):
    """Load and trim the cached OHLCV, or None if unusable."""
    path = find_ohlcv(ticker)
    if not path:
        return None
    try:
        import pandas as pd

        df = pd.read_csv(path, on_bad_lines="skip", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - a chart is never worth failing on
        log.warning("report_charts: cannot read %s: %s", path, exc)
        return None
    if df.empty or "Close" not in df.columns or "Date" not in df.columns:
        return None
    import pandas as pd

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"]).sort_values("Date")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Close"])
    if len(df) < 30:
        return None
    # Moving averages need history from *before* the visible window, so compute
    # on the full series and trim afterwards -- trimming first would leave the
    # 200-day line empty for the whole chart.
    df["SMA50"] = df["Close"].rolling(50).mean()
    df["SMA200"] = df["Close"].rolling(200).mean()
    df["RSI"] = _rsi(df["Close"])
    cutoff = df["Date"].max() - pd.DateOffset(months=months)
    return df[df["Date"] >= cutoff].reset_index(drop=True)


def _fig_png(fig) -> bytes:
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def price_panel(ticker: str, df) -> Optional[bytes]:
    """Price with moving averages, volume, and RSI -- the indicators the market
    analyst actually discusses."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError as exc:
        log.warning("report_charts: matplotlib unavailable: %s", exc)
        return None

    try:
        with plt.rc_context(_RC):
            fig, (ax1, ax2, ax3) = plt.subplots(
                3, 1, figsize=(7.0, 4.6), sharex=True,
                gridspec_kw={"height_ratios": [3, 1, 1.1], "hspace": 0.12},
            )
            d = df["Date"]
            ax1.plot(d, df["Close"], color=_PRICE, linewidth=1.3, label="Close", zorder=3)
            if df["SMA50"].notna().any():
                ax1.plot(d, df["SMA50"], color=_SMA50, linewidth=1.0, label="50-day MA")
            if df["SMA200"].notna().any():
                ax1.plot(d, df["SMA200"], color=_SMA200, linewidth=1.0, label="200-day MA")
            last = float(df["Close"].iloc[-1])
            ax1.axhline(last, color=_MUTED, linewidth=0.6, linestyle=":", zorder=1)
            ax1.annotate(f" {last:,.2f}", (d.iloc[-1], last), color=_PRICE,
                         fontsize=7.5, fontweight="bold", va="center")
            ax1.set_title(f"{ticker} — price, moving averages, volume and RSI")
            ax1.set_ylabel("Price")
            ax1.legend(loc="upper left", ncol=3)

            if "Volume" in df.columns and df["Volume"].notna().any():
                # Two years is ~500 bars across 7 inches, so per-bar colour
                # coding is unreadable mud -- one muted series plus its 20-day
                # average is what actually reads at this density.
                ax2.bar(d, df["Volume"], color="#94a3b8", width=1.0,
                        alpha=0.55, linewidth=0)
                ax2.plot(d, df["Volume"].rolling(20).mean(), color=_INK,
                         linewidth=0.9, label="20-day avg")
                ax2.set_ylabel("Volume")
                ax2.legend(loc="upper left")
                ax2.yaxis.set_major_formatter(
                    plt.FuncFormatter(lambda v, _: f"{v/1e6:.0f}M" if v else "0"))

            ax3.plot(d, df["RSI"], color="#0f766e", linewidth=1.0)
            ax3.axhline(70, color=_DOWN, linewidth=0.6, linestyle="--")
            ax3.axhline(30, color=_UP, linewidth=0.6, linestyle="--")
            ax3.fill_between(d, 30, 70, color=_GRID, alpha=0.35, zorder=0)
            ax3.set_ylim(0, 100)
            ax3.set_yticks([30, 50, 70])
            ax3.set_ylabel("RSI(14)")
            ax3.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
            for ax in (ax1, ax2, ax3):
                ax.margins(x=0.01)
            return _fig_png(fig)
    except Exception as exc:  # noqa: BLE001 - never fail a report over a chart
        log.error("report_charts: price_panel failed for %s: %s", ticker, exc, exc_info=True)
        return None


_TARGET_RE = re.compile(
    r"(?:price\s*target|目标价|目標價)\s*(?:\*\*)?\s*[:：]?\s*\$?\s*([0-9][0-9,]*\.?[0-9]*)",
    re.IGNORECASE)


def parse_price_target(text: str) -> Optional[float]:
    """Pull the portfolio manager's price target out of the report."""
    for m in _TARGET_RE.finditer(text or ""):
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        # Sanity bound: a "target" of 0 or 10**7 is a mis-parse, and drawing it
        # would squash the whole axis.
        if 0.01 < val < 1_000_000:
            return val
    return None


def position_bar(ticker: str, df, target: Optional[float]) -> Optional[bytes]:
    """Where the last price sits in its 52-week range, and versus the target."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import pandas as pd
    except ImportError:
        return None
    try:
        cutoff = df["Date"].max() - pd.DateOffset(months=12)
        yr = df[df["Date"] >= cutoff]
        if yr.empty:
            return None
        lo = float(yr["Low"].min() if "Low" in yr and yr["Low"].notna().any()
                   else yr["Close"].min())
        hi = float(yr["High"].max() if "High" in yr and yr["High"].notna().any()
                   else yr["Close"].max())
        last = float(df["Close"].iloc[-1])
        if not (hi > lo):
            return None

        with plt.rc_context(_RC):
            fig, ax = plt.subplots(figsize=(7.0, 1.15))
            ax.grid(False)
            ax.set_yticks([])
            for side in ("left", "right", "top", "bottom"):
                ax.spines[side].set_visible(False)

            # The axis starts at the 52-week range and may stretch by up to
            # 60% of that range to bring a nearby target into view. Beyond
            # that the target is labelled off-scale instead: letting an
            # optimistic number set the scale squashes the range -- the chart's
            # actual subject -- into an unreadable stub.
            span = hi - lo
            x0, x1 = lo - span * 0.12, hi + span * 0.12
            if target and target > 0:
                room = span * 0.6
                if x1 < target <= hi + room:
                    x1 = target + span * 0.12
                elif lo - room <= target < x0:
                    x0 = target - span * 0.12
            ax.set_xlim(x0, x1)
            ax.set_ylim(-0.75, 0.85)

            ax.hlines(0, lo, hi, color=_GRID, linewidth=9, zorder=1)
            ax.hlines(0, lo, last, color=_PRICE, linewidth=9, alpha=0.35, zorder=2)
            ax.plot([last], [0], marker="D", color=_PRICE, markersize=7, zorder=4)
            ax.annotate(f"last {last:,.2f}", (last, 0), textcoords="offset points",
                        xytext=(0, 11), ha="center", color=_PRICE,
                        fontsize=7.5, fontweight="bold")
            # Anchored to the actual low and high, so the label sits under the
            # end of the bar it describes even when the axis has been stretched
            # for a target. Safe from collision because the stretch is bounded,
            # which leaves the range occupying most of the axis.
            ax.annotate(f"52w low {lo:,.2f}", (lo, 0), textcoords="offset points",
                        xytext=(0, -16), ha="center", color=_MUTED, fontsize=7)
            ax.annotate(f"52w high {hi:,.2f}", (hi, 0), textcoords="offset points",
                        xytext=(0, -16), ha="center", color=_MUTED, fontsize=7)

            if target and target > 0:
                colour = _UP if target >= last else _DOWN
                pct = (target / last - 1) * 100
                if x0 <= target <= x1:
                    ax.plot([target], [0], marker="v", color=colour,
                            markersize=8, zorder=5)
                    ax.annotate(f"target {target:,.2f} ({pct:+.0f}%)", (target, 0),
                                textcoords="offset points", xytext=(0, 13),
                                ha="center", color=colour, fontsize=7.5,
                                fontweight="bold")
                else:
                    # Off-scale: say so rather than silently clipping the marker
                    # to the edge, which would read as "target = 52w high".
                    at_right = target > x1
                    ax.annotate(
                        f"target {target:,.2f} ({pct:+.0f}%) {'▶' if at_right else '◀'} off scale",
                        (x1 if at_right else x0, 0), textcoords="offset points",
                        xytext=(-2 if at_right else 2, 24),
                        ha="right" if at_right else "left",
                        color=colour, fontsize=7.5, fontweight="bold")
            ax.set_title(f"{ticker} — 52-week range and price target", loc="left")
            return _fig_png(fig)
    except Exception as exc:  # noqa: BLE001
        log.error("report_charts: position_bar failed for %s: %s", ticker, exc, exc_info=True)
        return None


def build_all(ticker: str, report_text: str) -> list[dict[str, Any]]:
    """Charts for a report, in display order. Empty when data is unavailable.

    Each spec carries its caption in both languages, because the PDF picks its
    language from the report's own content -- a Chinese report with an English
    figure caption reads like a screenshot pasted in from somewhere else.

    Never raises: a report with no picture is worth far more than no report.
    """
    try:
        df = load_frame(ticker)
    except Exception as exc:  # noqa: BLE001
        log.warning("report_charts: load_frame failed for %s: %s", ticker, exc)
        return []
    if df is None or df.empty:
        log.info("report_charts: no cached OHLCV for %s; PDF will be text-only", ticker)
        return []

    out: list[dict[str, Any]] = []
    png = price_panel(ticker, df)
    if png:
        out.append({"png": png, "w": 7.0, "h": 4.6,
                    "caption": "Price with 50- and 200-day moving averages, "
                               "daily volume with its 20-day average, and "
                               "RSI(14) with the 30/70 bands. Drawn from the "
                               "same daily bars the agents analysed.",
                    "caption_zh": "收盘价与 50 日、200 日均线，成交量及其 20 日均量，"
                                  "以及 RSI(14) 与 30/70 阈值带。数据取自智能体本次"
                                  "分析所用的同一份日线行情。"})
    bar = position_bar(ticker, df, parse_price_target(report_text))
    if bar:
        out.append({"png": bar, "w": 7.0, "h": 1.15,
                    "caption": "Last close within its 52-week range, against "
                               "the price target stated in the report.",
                    "caption_zh": "最新收盘价在 52 周区间中的位置，并与报告给出的"
                                  "目标价对比。"})
    return out
