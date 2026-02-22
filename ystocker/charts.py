"""
ystocker.charts
~~~~~~~~~~~~~~~
All chart-generation functions.  Each function returns a base-64-encoded PNG
string that can be embedded directly in HTML as <img src="data:image/png;base64,...">

This keeps the web server stateless - no chart files are written to disk.
"""
from __future__ import annotations

import base64
import io
from typing import Dict, List

import matplotlib
matplotlib.use("Agg")  # non-interactive backend - required for server use
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import seaborn as sns

sns.set_theme(style="whitegrid", palette="muted")
plt.rcParams["figure.dpi"] = 110


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fig_to_b64(fig: plt.Figure) -> str:
    """Render *fig* to a PNG and return it as a base-64 string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def _to_numeric_df(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    """Return a copy of *df* with *cols* coerced to float (None → NaN)."""
    out = df[cols].copy()
    for c in cols:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


# ---------------------------------------------------------------------------
# Public chart functions
# ---------------------------------------------------------------------------

def chart_pe_peg_bars(group_name: str, df: pd.DataFrame) -> str:
    """
    Dual-axis grouped bar chart.
    Left axis  → PE (TTM) and PE (Forward)
    Right axis → PEG ratio

    Returns a base-64 PNG string.
    """
    subset = _to_numeric_df(df, ["PE (TTM)", "PE (Forward)", "PEG"]).dropna(how="all")
    if subset.empty:
        return ""

    fig, ax1 = plt.subplots(figsize=(9, 4.5))
    ax2 = ax1.twinx()

    x     = list(range(len(subset)))
    width = 0.28

    ttm_vals = subset["PE (TTM)"].fillna(0).tolist()
    fwd_vals = subset["PE (Forward)"].fillna(0).tolist()
    peg_vals = subset["PEG"].fillna(0).tolist()

    bars_ttm = ax1.bar([i - width for i in x], ttm_vals, width=width,
                       label="PE (TTM)",    color="#4C72B0", edgecolor="white")
    bars_fwd = ax1.bar(x, fwd_vals,         width=width,
                       label="PE (Forward)", color="#DD8452", edgecolor="white")
    bars_peg = ax2.bar([i + width for i in x], peg_vals, width=width,
                       label="PEG",          color="#55A868", edgecolor="white", alpha=0.85)

    for col, ls in zip(["PE (TTM)", "PE (Forward)"], ["--", ":"]):
        med = subset[col].median()
        if not pd.isna(med):
            ax1.axhline(med, linestyle=ls, linewidth=1.0, color="grey")

    ax2.axhline(1, linestyle="-.", linewidth=1.0, color="green", alpha=0.6)

    for bar_set, originals, ax in [
        (bars_ttm, subset["PE (TTM)"].tolist(),    ax1),
        (bars_fwd, subset["PE (Forward)"].tolist(), ax1),
        (bars_peg, subset["PEG"].tolist(),          ax2),
    ]:
        for bar, val in zip(bar_set, originals):
            if not pd.isna(val) and val != 0:
                ax.text(bar.get_x() + bar.get_width() / 2, val + 0.5,
                        f"{val:.1f}", ha="center", va="bottom", fontsize=7.5)

    ax1.set_xticks(x)
    ax1.set_xticklabels(subset.index, rotation=0)
    ax1.set_ylabel("PE Ratio", fontsize=10)
    ax2.set_ylabel("PEG Ratio", fontsize=10, color="#55A868")
    ax2.tick_params(axis="y", labelcolor="#55A868")
    ax1.set_title(f"{group_name} - PE (TTM / Forward) & PEG", fontsize=13, fontweight="bold")

    handles = [bars_ttm, bars_fwd, bars_peg,
               plt.Line2D([0], [0], linestyle="-.", color="green", alpha=0.6)]
    ax1.legend(handles, ["PE (TTM)", "PE (Forward)", "PEG", "PEG = 1"],
               fontsize=8, frameon=False, loc="upper left")

    return _fig_to_b64(fig)


def chart_price_vs_target(group_name: str, df: pd.DataFrame) -> str:
    """Bar chart of current prices with analyst target overlaid as scatter + arrows."""
    sub = _to_numeric_df(df, ["Current Price", "Target Price"]).dropna()
    if sub.empty:
        return ""

    x   = list(range(len(sub)))
    fig, ax = plt.subplots(figsize=(8, 4))

    ax.bar(x, sub["Current Price"], width=0.4, label="Current Price", align="center", alpha=0.85)
    ax.scatter(x, sub["Target Price"], marker="D", s=80, color="tomato", zorder=5, label="Analyst Target")

    for i, (_, row) in enumerate(sub.iterrows()):
        ax.annotate("", xy=(i, row["Target Price"]), xytext=(i, row["Current Price"]),
                    arrowprops=dict(arrowstyle="->", color="tomato", lw=1.5))

    ax.set_xticks(x)
    ax.set_xticklabels(sub.index, rotation=0)
    ax.set_ylabel("USD")
    ax.set_title(f"{group_name} - Current Price vs Analyst 12-month Target",
                 fontsize=13, fontweight="bold")
    ax.legend(frameon=False)
    ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("${x:,.0f}"))

    return _fig_to_b64(fig)


def chart_upside(group_name: str, df: pd.DataFrame) -> str:
    """Horizontal bar chart of analyst upside %, colour-coded green/red."""
    sub = _to_numeric_df(df, ["Upside (%)"]).dropna().sort_values("Upside (%)")
    if sub.empty:
        return ""

    colors = ["#d62728" if v < 0 else "#2ca02c" for v in sub["Upside (%)"]]
    fig, ax = plt.subplots(figsize=(7, 3.5))
    bars = ax.barh(sub.index, sub["Upside (%)"], color=colors, edgecolor="white")
    ax.axvline(0, color="black", linewidth=0.8)
    ax.bar_label(bars, fmt="%.1f%%", padding=4, fontsize=9)
    ax.set_xlabel("Upside to Analyst Target (%)")
    ax.set_title(f"{group_name} - Implied Upside to Analyst Target",
                 fontsize=13, fontweight="bold")

    return _fig_to_b64(fig)


def chart_peg_bars(group_name: str, df: pd.DataFrame) -> str:
    """Horizontal PEG ratio bar chart, colour-coded by valuation zone."""
    sub = _to_numeric_df(df, ["PEG"]).dropna().sort_values("PEG")
    if sub.empty:
        return ""

    colors = ["#2ca02c" if v < 1 else ("#ff7f0e" if v < 2 else "#d62728")
              for v in sub["PEG"]]

    fig, ax = plt.subplots(figsize=(7, 3.5))
    bars = ax.barh(sub.index, sub["PEG"], color=colors, edgecolor="white")
    ax.axvline(1, color="black",  linewidth=1.0, linestyle="--", label="PEG = 1")
    ax.axvline(2, color="grey",   linewidth=0.8, linestyle=":",  label="PEG = 2")
    ax.bar_label(bars, fmt="%.2f", padding=4, fontsize=9)
    ax.set_xlabel("PEG Ratio")
    ax.set_title(f"{group_name} - PEG Ratios", fontsize=13, fontweight="bold")
    ax.legend(fontsize=8, frameon=False)

    return _fig_to_b64(fig)


def chart_heatmap(all_dfs: Dict[str, pd.DataFrame]) -> str:
    """PE & PEG heatmap across all unique tickers from every peer group."""
    rows = []
    for df in all_dfs.values():
        rows.append(df[["PE (TTM)", "PE (Forward)", "PEG"]].copy())

    combined   = pd.concat(rows)
    combined   = combined[~combined.index.duplicated(keep="first")]
    heat_data  = combined.apply(pd.to_numeric, errors="coerce")

    fig, ax = plt.subplots(figsize=(6, len(heat_data) * 0.45 + 1))
    sns.heatmap(heat_data, annot=True, fmt=".1f", cmap="RdYlGn_r",
                linewidths=0.5, ax=ax, cbar_kws={"label": "Ratio value"})
    ax.set_title("PE & PEG Heatmap - all tickers", fontsize=13, fontweight="bold")
    ax.set_ylabel("")

    return _fig_to_b64(fig)


def chart_scatter(all_dfs: Dict[str, pd.DataFrame]) -> str:
    """Forward PE vs analyst upside scatter - one point per unique ticker."""
    palette = {"Tech": "#1f77b4", "Cloud / SaaS": "#ff7f0e", "Semiconductors": "#2ca02c"}
    seen: set[str] = set()

    fig, ax = plt.subplots(figsize=(9, 6))

    for sector, df in all_dfs.items():
        sub = _to_numeric_df(df, ["PE (Forward)", "Upside (%)"]).dropna()
        for ticker, row in sub.iterrows():
            if ticker in seen:
                continue
            seen.add(ticker)
            ax.scatter(row["PE (Forward)"], row["Upside (%)"],
                       s=120, color=palette.get(sector, "grey"),
                       zorder=3, edgecolors="white", linewidths=0.8,
                       label=sector if ticker == sub.index[0] else "_nolegend_")
            ax.annotate(ticker, (row["PE (Forward)"], row["Upside (%)"]),
                        textcoords="offset points", xytext=(6, 4), fontsize=8)

    ax.axhline(0, color="grey", linewidth=0.8, linestyle="--")
    ax.set_xlabel("Forward PE Ratio",            fontsize=11)
    ax.set_ylabel("Upside to Analyst Target (%)", fontsize=11)
    ax.set_title("Valuation Map - Forward PE vs Analyst Upside",
                 fontsize=13, fontweight="bold")

    # De-duplicate legend entries
    handles, labels = ax.get_legend_handles_labels()
    by_label = dict(zip(labels, handles))
    ax.legend(by_label.values(), by_label.keys(), frameon=False)

    return _fig_to_b64(fig)
