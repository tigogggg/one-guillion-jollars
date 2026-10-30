"""Weekly summary for the Discord channel, via yfinance.

- Week's change of the main indices and of each S&P 500 sector, with chart.
- Breadth: share of S&P 500 stocks above their 50-day average, now vs a week ago.
- Biggest S&P 500 gainers and losers of the week.
- Track record of the opportunity alerts sent in the last 5 weeks
  (state/history/alerts.json, shared through the Actions cache).
- Next week's earnings of the stocks the channel follows: the latest
  rankings, recent alerts and the agents' holdings.

    uv run python scripts/weekly_summary.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

import pandas as pd

import earnings
import macro
from charts import returns_chart
from discord_daily import WATCHLIST_DIR
from discord_post import BLUE, GREEN, RED, embed, send
from opportunity_alerts import HISTORY_PATH
from rankings import SECTORS_PT, download_history, load_constituents

INDICES = [
    ("SPY", "S&P 500"),
    ("QQQ", "Nasdaq 100"),
    ("IWM", "Russell 2000"),
    ("EUNL.DE", "MSCI World (€)"),
    ("SXRT.DE", "Euro Stoxx 50 (€)"),
    ("4GLD.DE", "Ouro (€)"),
]
SECTOR_ETFS = {
    "XLK": "Information Technology",
    "XLV": "Health Care",
    "XLF": "Financials",
    "XLY": "Consumer Discretionary",
    "XLC": "Communication Services",
    "XLI": "Industrials",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLB": "Materials",
}
AGENTS_CONFIG = WATCHLIST_DIR.parents[1] / "agents" / "config.json"
AGENTS_STATE = WATCHLIST_DIR.parents[1] / "agents" / "state.json"
TRACK_RECORD_DAYS = 35
WEEK = 5  # trading sessions


def change(df: pd.DataFrame, sessions: int) -> float | None:
    close = df["Close"]
    return float(close.iloc[-1] / close.iloc[-sessions - 1] - 1) if len(close) > sessions else None


def share_above_sma50(histories: dict[str, pd.DataFrame], offset: int = 0) -> float | None:
    """% of symbols closing above their 50-day average, `offset` sessions ago."""
    above = total = 0
    for df in histories.values():
        close = df["Close"].iloc[: len(df) - offset] if offset else df["Close"]
        if len(close) < 50:
            continue
        total += 1
        above += close.iloc[-1] > close.iloc[-50:].mean()
    return 100 * above / total if total else None


def track_record(history: list[dict], prices: dict[str, pd.DataFrame], today: date) -> tuple[list[str], dict]:
    """One line per recent alert: change since the alert, and whether the stop was hit."""
    since = today - timedelta(days=TRACK_RECORD_DAYS)
    lines, stats = [], {"total": 0, "up": 0, "stopped": 0}
    for a in history:
        if date.fromisoformat(a["date"]) < since or a["symbol"] not in prices:
            continue
        df = prices[a["symbol"]]
        after = df[df.index.date >= date.fromisoformat(a["date"])]
        if after.empty:
            continue
        last = float(after["Close"].iloc[-1])
        stopped = float(after["Low"].min()) <= a["stop"]
        move = last / a["price"] - 1
        stats["total"] += 1
        stats["up"] += move > 0
        stats["stopped"] += stopped
        status = "🛑 stop atingido" if stopped else ("🟢" if move > 0 else "🔴")
        lines.append(f"**{a['symbol']}** ({a['date'][5:]}) {move * 100:+.1f}% desde o alerta · {status}")
    return lines, stats


def followed_symbols(history: list[dict], today: date) -> tuple[set[str], dict[str, list[str]]]:
    """Stocks in the latest rankings, alerts of the last 2 weeks and agents' holdings (with holder names)."""
    symbols: set[str] = set()
    rankings = WATCHLIST_DIR / "rankings-latest.json"
    if rankings.exists():
        symbols |= {r["symbol"] for rows in json.loads(rankings.read_text())["stocks"].values() for r in rows}
    symbols |= {a["symbol"] for a in history if date.fromisoformat(a["date"]) >= today - timedelta(days=14)}
    holders: dict[str, list[str]] = {}
    if AGENTS_STATE.exists():
        names = {a["id"]: a["emoji"] for a in json.loads(AGENTS_CONFIG.read_text())["agents"]}
        for agent_id, port in json.loads(AGENTS_STATE.read_text())["agents"].items():
            for s in port["positions"]:
                holders.setdefault(s, []).append(names.get(agent_id, "🤖"))
    symbols |= set(holders)
    return {s for s in symbols if not s.endswith(earnings.NO_EARNINGS_SUFFIXES)}, holders


def earnings_field(symbols: set[str], holders: dict[str, list[str]], today: date) -> tuple[str, str] | None:
    monday, friday = earnings.next_week(today)
    dates = earnings.next_earnings_many(sorted(symbols), today)
    by_day: dict[date, list[str]] = {}
    for s, d in dates.items():
        if d and monday <= d <= friday:
            by_day.setdefault(d, []).append(f"**{s}**" + ("".join(holders[s]) if s in holders else ""))
    if not by_day:
        return None
    lines = [f"{earnings.label(d)}: " + ", ".join(sorted(syms)) for d, syms in sorted(by_day.items())]
    return (f"📅 Resultados na próxima semana (ações seguidas pelo canal; emoji = agente que a tem)", "\n".join(lines))


def fmt(x: float | None, decimals: int = 1) -> str:
    return "—" if x is None else f"{x * 100:+.{decimals}f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print the cards instead of posting")
    args = parser.parse_args()

    constituents = load_constituents()
    stocks = download_history([c["symbol"] for c in constituents], period="6mo")
    other = download_history([t for t, _ in INDICES] + list(SECTOR_ETFS), period="6mo")
    if "SPY" not in other:
        print("Sem dados do Yahoo Finance; nada enviado.", file=sys.stderr)
        return 1
    as_of = other["SPY"].index[-1].date()

    index_lines = [f"**{name}** {fmt(change(other[t], WEEK))} na semana · {fmt(change(other[t], 21))} no mês"
                   for t, name in INDICES if t in other]
    sectors = sorted(
        ((SECTORS_PT[s], change(other[t], WEEK)) for t, s in SECTOR_ETFS.items() if t in other),
        key=lambda x: x[1] or 0,
        reverse=True,
    )
    now, before = share_above_sma50(stocks), share_above_sma50(stocks, offset=WEEK)
    moves = {t: change(df, WEEK) for t, df in stocks.items()}
    moves = {t: m for t, m in moves.items() if m is not None}
    ranked = sorted(moves, key=moves.get, reverse=True)

    breadth = "—"
    if now is not None and before is not None:
        arrow = "📈" if now > before else "📉"
        breadth = f"{arrow} **{now:.0f}%** das ações do S&P 500 acima da média de 50 dias (há uma semana: {before:.0f}%)"
    spy_week = change(other["SPY"], WEEK) or 0
    summary = embed(
        f"🗓️ Resumo semanal (semana até {as_of.isoformat()})",
        "\n".join([*index_lines, "", breadth]),
        color=GREEN if spy_week >= 0 else RED,
        fields=[
            ("🚀 Maiores subidas do S&P 500", " · ".join(f"**{t}** {fmt(moves[t], 0)}" for t in ranked[:5])),
            ("🔻 Maiores quedas do S&P 500", " · ".join(f"**{t}** {fmt(moves[t], 0)}" for t in ranked[-5:][::-1])),
        ],
        image="setores-semana.png",
    )
    monday, friday = earnings.next_week(as_of)
    macro_events, from_fmp = macro.events(monday, friday)
    if macro_events:
        source = "calendário económico FMP" if from_fmp else "datas da Fed e regra habitual do emprego"
        summary["fields"].append({"name": f"🏛️ Agenda macro da próxima semana ({source})",
                                  "value": "\n".join(macro.by_day_lines(macro_events))[:1024], "inline": False})
    symbols, holders = followed_symbols(json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else [], as_of)
    upcoming = earnings_field(symbols, holders, as_of)
    if upcoming:
        summary["fields"].append({"name": upcoming[0][:256], "value": upcoming[1][:1024], "inline": False})
    files = {"setores-semana.png": returns_chart([(n, (m or 0) * 100) for n, m in sectors], "Variação semanal por setor (ETFs SPDR)")}
    embeds = [summary]

    history = json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else []
    lines, stats = track_record(history, stocks, as_of)
    if lines:
        embeds.append(
            embed(
                f"🎯 Alertas das últimas {TRACK_RECORD_DAYS // 7} semanas: como correram",
                "\n".join(lines[-15:])
                + f"\n\n**{stats['up']} de {stats['total']}** acima do preço do alerta"
                f" · **{stats['stopped']}** chegaram ao stop",
                color=BLUE,
                footer="Preço de fecho de hoje vs preço no momento do alerta. Resultados passados não garantem nada.",
            )
        )
    return send(embeds, files, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
