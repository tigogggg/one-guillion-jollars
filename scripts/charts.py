"""PNG charts for the Discord posts, styled for Discord's dark theme."""

from __future__ import annotations

import io

import matplotlib
import matplotlib.dates
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

BG = "#2b2d31"
FG = "#dbdee1"
GRID = "#3f4147"
PRICE = "#f2f3f5"
SMA50 = "#4cc0a8"
SMA200 = "#e2b84a"
PIVOT = "#5865f2"
STOP = "#ed4245"
UP = "#3ba55d"
DOWN = "#d83c3e"
NEUTRAL = "#80848e"


def _figure(height: float = 4.5):
    fig = plt.figure(figsize=(8, height), dpi=110, facecolor=BG)
    return fig


def _style(ax) -> None:
    ax.set_facecolor(BG)
    ax.tick_params(colors=FG, labelsize=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def price_chart(
    df: pd.DataFrame,
    title: str,
    pivot: float | None = None,
    stop: float | None = None,
    days: int = 130,
) -> bytes:
    """Close with 50/200-day averages, optional pivot and stop lines, and volume."""
    close = df["Close"]
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    view = df.iloc[-days:]

    fig = _figure()
    grid = fig.add_gridspec(4, 1, hspace=0.05)
    ax = fig.add_subplot(grid[:3, 0])
    vol = fig.add_subplot(grid[3, 0], sharex=ax)
    for a in (ax, vol):
        _style(a)

    ax.plot(view.index, view["Close"], color=PRICE, linewidth=1.6, label="Preço")
    ax.plot(view.index, sma50.loc[view.index], color=SMA50, linewidth=1.1, label="Média 50d")
    ax.plot(view.index, sma200.loc[view.index], color=SMA200, linewidth=1.1, label="Média 200d")
    if pivot:
        ax.axhline(pivot, color=PIVOT, linestyle="--", linewidth=1.1, label=f"Pivot {pivot:.2f}")
    if stop:
        ax.axhline(stop, color=STOP, linestyle="--", linewidth=1.1, label=f"Stop {stop:.2f}")
    ax.set_title(title, color=FG, fontsize=11, loc="left", fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, facecolor=BG, edgecolor=GRID, labelcolor=FG, ncol=5)
    ax.tick_params(labelbottom=False)

    colors = [UP if c >= o else DOWN for c, o in zip(view["Close"], view["Close"].shift(1).fillna(view["Close"]))]
    vol.bar(view.index, view["Volume"], color=colors, width=1.0, alpha=0.7)
    vol.set_yticks([])
    fig.autofmt_xdate(rotation=0, ha="center")
    return _png(fig)


def sector_chart(sectors: list[tuple[str, float, bool]], overbought: float = 37.0, oversold: float = 9.7) -> bytes:
    """Horizontal bars of % of stocks in uptrend per sector: (name, pct, trend_up)."""
    rows = sorted(sectors, key=lambda s: s[1])
    fig = _figure(height=0.32 * len(rows) + 1.0)
    ax = fig.add_subplot(1, 1, 1)
    _style(ax)
    ax.barh([r[0] for r in rows], [r[1] for r in rows], color=[UP if r[2] else DOWN for r in rows], height=0.65)
    for i, r in enumerate(rows):
        ax.text(r[1] + 0.6, i, f"{r[1]:.1f}%", va="center", color=FG, fontsize=8)
    ax.axvline(overbought, color=SMA200, linestyle="--", linewidth=1, label=f"Sobrecomprado {overbought:g}%")
    ax.axvline(oversold, color=STOP, linestyle="--", linewidth=1, label=f"Sobrevendido {oversold:g}%")
    ax.set_xlim(0, max(overbought, max(r[1] for r in rows)) * 1.15)
    ax.set_title("% de ações em tendência de alta por setor (verde = a subir)", color=FG, fontsize=10, loc="left")
    ax.legend(loc="lower right", fontsize=7, facecolor=BG, edgecolor=GRID, labelcolor=FG)
    ax.grid(axis="y", visible=False)
    return _png(fig)


def returns_chart(rows: list[tuple[str, float]], title: str) -> bytes:
    """Horizontal bars of returns in %, best on top: (label, pct)."""
    rows = list(reversed(rows))
    fig = _figure(height=0.32 * len(rows) + 1.0)
    ax = fig.add_subplot(1, 1, 1)
    _style(ax)
    values = [r[1] for r in rows]
    ax.barh([r[0] for r in rows], values, color=[UP if v >= 0 else DOWN for v in values], height=0.65)
    span = max(abs(v) for v in values) or 1
    for i, v in enumerate(values):
        ax.text(v + (span * 0.02 if v >= 0 else -span * 0.02), i, f"{v:+.0f}%", va="center",
                ha="left" if v >= 0 else "right", color=FG, fontsize=8)
    ax.axvline(0, color=NEUTRAL, linewidth=0.8)
    ax.set_xlim(min(0, min(values)) - span * 0.15, max(0, max(values)) + span * 0.2)
    ax.set_title(title, color=FG, fontsize=10, loc="left")
    ax.grid(axis="y", visible=False)
    return _png(fig)


REFERENCE_STYLES = [("--", NEUTRAL), (":", "#b5bac1")]


def lines_chart(series: dict[str, list[tuple]], title: str, references: dict[str, list[tuple]] | None = None) -> bytes:
    """One line per name of (date, % value), with a zero line and end labels.

    references (e.g. inflation, deposit) are drawn as thin grey dashed lines so
    they read as yardsticks, not as competitors.
    """
    palette = ["#4cc0a8", "#e2b84a", "#5865f2", "#ed4245", "#57f287", "#eb459e", "#f0b232", "#9b84ee", "#3ba5d8", "#dbdee1"]
    fig = _figure()
    ax = fig.add_subplot(1, 1, 1)
    _style(ax)
    ax.axhline(0, color=NEUTRAL, linewidth=0.8)
    ends = []
    for i, (name, points) in enumerate(series.items()):
        if not points:
            continue
        dates = pd.to_datetime([d for d, _ in points])
        values = [v for _, v in points]
        color = palette[i % len(palette)]
        ax.plot(dates, values, color=color, linewidth=1.4, marker="o" if len(points) < 3 else None, markersize=3)
        ends.append([values[-1], values[-1], dates[-1], f"{name} {values[-1]:+.1f}%", color])
    for i, (name, points) in enumerate((references or {}).items()):
        if not points:
            continue
        dates = pd.to_datetime([d for d, _ in points])
        values = [v for _, v in points]
        style, color = REFERENCE_STYLES[i % len(REFERENCE_STYLES)]
        ax.plot(dates, values, color=color, linewidth=1.0, linestyle=style)
        ends.append([values[-1], values[-1], dates[-1], f"{name} {values[-1]:+.1f}%", color])
    # End labels, nudged apart so lines that finish close together stay readable.
    low, high = ax.get_ylim()
    gap = (high - low) * 0.045
    ends.sort(key=lambda e: e[0])
    for prev, cur in zip(ends, ends[1:]):
        cur[1] = max(cur[1], prev[1] + gap)
    for _, y, x, label, color in ends:
        ax.annotate(label, (x, y), xytext=(4, 0), textcoords="offset points", color=color, fontsize=7, va="center")
    ax.set_title(title, color=FG, fontsize=10, loc="left")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:+.1f}%"))
    dates_all = pd.to_datetime(sorted({d for pts in series.values() for d, _ in pts}))
    span_days = (dates_all[-1] - dates_all[0]).days if len(dates_all) else 0
    if span_days > 2 * 365:  # years: "2008", "2010", …
        ax.xaxis.set_major_locator(matplotlib.dates.YearLocator(base=max(1, round(span_days / 365 / 10))))
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%Y"))
    elif span_days > 120:  # months: "nov 26"
        ax.xaxis.set_major_locator(matplotlib.dates.MonthLocator(interval=max(1, round(span_days / 30 / 8))))
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m/%y"))
    else:  # days: "02/11"
        ax.xaxis.set_major_locator(matplotlib.dates.AutoDateLocator(minticks=2, maxticks=8))
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d/%m"))
        if len(dates_all) < 3:
            ax.xaxis.set_major_locator(matplotlib.dates.DayLocator())
    if len(dates_all):  # room on the right for the end labels, none on the left, no ticks past the data
        ax.set_xlim(dates_all[0], dates_all[-1] + pd.Timedelta(days=max(1, span_days) * 0.2))
        last = matplotlib.dates.date2num(dates_all[-1])
        ax.set_xticks([t for t in ax.get_xticks() if t <= last])
    return _png(fig)
