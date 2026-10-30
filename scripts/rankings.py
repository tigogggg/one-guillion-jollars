"""Daily rankings for the Discord channel, from Yahoo Finance via yfinance.

- Top 3 S&P 500 stocks per sector by trend + quality.
- Top 10 UCITS ETFs (buyable from EU brokers) by trend.

Scores rank against fixed criteria; they are not buy recommendations. The
result is also saved to state/watchlist/ for the intraday alerts.

    uv run python scripts/rankings.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from datetime import date

import pandas as pd
import requests
import yfinance as yf

import earnings
from charts import returns_chart
from discord_daily import WATCHLIST_DIR
from discord_post import BLUE, DISCLAIMER, GREEN, embed, send

CONSTITUENTS_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)
BENCHMARK = "SPY"
TOP_PER_SECTOR = 3
FUNDAMENTALS_PER_SECTOR = 8
TOP_ETFS = 10
MIN_DOLLAR_VOLUME = 20_000_000
TREND_WEIGHT, QUALITY_WEIGHT = 0.6, 0.4
EARNINGS_SOON_DAYS = 10

SECTORS_PT = {
    "Information Technology": "Tecnologia",
    "Health Care": "Saúde",
    "Financials": "Financeiro",
    "Consumer Discretionary": "Consumo cíclico",
    "Communication Services": "Comunicações",
    "Industrials": "Industrial",
    "Consumer Staples": "Consumo básico",
    "Energy": "Energia",
    "Utilities": "Utilities",
    "Real Estate": "Imobiliário",
    "Materials": "Materiais",
}

# UCITS ETFs (and one gold ETC) buyable from EU brokers. Xetra (.DE) listings
# where possible so prices are in EUR. Tickers Yahoo does not know are skipped
# and counted in the message footer.
UCITS_ETFS = [
    ("VWCE.DE", "Vanguard FTSE All-World"),
    ("EUNL.DE", "iShares Core MSCI World"),
    ("IUSQ.DE", "iShares MSCI ACWI"),
    ("SXR8.DE", "iShares Core S&P 500"),
    ("VUAA.DE", "Vanguard S&P 500"),
    ("SXRV.DE", "iShares Nasdaq 100"),
    ("ZPRR.DE", "SPDR Russell 2000 US Small Cap"),
    ("IUSN.DE", "iShares MSCI World Small Cap"),
    ("IS3N.DE", "iShares Core MSCI EM IMI"),
    ("QDV5.DE", "iShares MSCI India"),
    ("SXRT.DE", "iShares Core EURO STOXX 50"),
    ("EXSA.DE", "iShares STOXX Europe 600"),
    ("EXS1.DE", "iShares Core DAX"),
    ("SXR7.DE", "iShares Core MSCI EMU"),
    ("XDWT.DE", "Xtrackers MSCI World Tecnologia"),
    ("XDWH.DE", "Xtrackers MSCI World Saúde"),
    ("XDWF.DE", "Xtrackers MSCI World Financeiro"),
    ("XDW0.DE", "Xtrackers MSCI World Energia"),
    ("XDWI.DE", "Xtrackers MSCI World Industrial"),
    ("XDWC.DE", "Xtrackers MSCI World Consumo cíclico"),
    ("XDWS.DE", "Xtrackers MSCI World Consumo básico"),
    ("XDWM.DE", "Xtrackers MSCI World Materiais"),
    ("XDWU.DE", "Xtrackers MSCI World Utilities"),
    ("XWTS.DE", "Xtrackers MSCI World Comunicações"),
    ("VVSM.DE", "VanEck Semicondutores"),
    ("XAIX.DE", "Xtrackers IA & Big Data"),
    ("2B76.DE", "iShares Automação & Robótica"),
    ("USPY.DE", "L&G Cibersegurança"),
    ("DFEN.DE", "VanEck Defesa"),
    ("NUCL.DE", "VanEck Urânio e Nuclear"),
    ("IQQH.DE", "iShares Energia Limpa Global"),
    ("IS3Q.DE", "iShares MSCI World Quality"),
    ("IS3R.DE", "iShares MSCI World Momentum"),
    ("IS3S.DE", "iShares MSCI World Value"),
    ("VHYL.DE", "Vanguard All-World High Dividend"),
    ("TDIV.AS", "VanEck Dividend Leaders"),
    ("VAGF.DE", "Vanguard Global Aggregate Bond (EUR)"),
    ("EUNH.DE", "iShares Core Euro Govt Bond"),
    ("4GLD.DE", "Xetra-Gold (ETC ouro)"),
]


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def load_constituents() -> list[dict]:
    resp = requests.get(CONSTITUENTS_URL, timeout=30)
    resp.raise_for_status()
    return [
        # Yahoo uses dash-style class shares (BRK-B); the dataset uses dots.
        {"symbol": row["Symbol"].replace(".", "-"), "name": row["Security"], "sector": row["GICS Sector"]}
        for row in csv.DictReader(io.StringIO(resp.text))
    ]


def download_history(tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    """Daily OHLCV per ticker; tickers Yahoo returns nothing for are left out."""
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), 100):
        chunk = tickers[i : i + 100]
        df = yf.download(
            chunk,
            period=period,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
        if df is None or df.empty:
            continue
        available = set(df.columns.get_level_values(0))
        for t in chunk:
            if t in available:
                sub = df[t].dropna(subset=["Close"])
                if len(sub):
                    out[t] = sub
    return out


def trend_metrics(df: pd.DataFrame) -> dict | None:
    """Trend and liquidity figures from at least 200 daily bars (oldest first)."""
    close = df["Close"]
    if len(close) < 220:
        return None
    price = float(close.iloc[-1])

    def ret(n: int) -> float:
        return price / float(close.iloc[-n - 1]) - 1

    high52 = float(df["High"].iloc[-252:].max())
    low52 = float(df["Low"].iloc[-252:].min())
    sma200 = float(close.iloc[-200:].mean())
    return {
        "price": price,
        "sma50": float(close.iloc[-50:].mean()),
        "sma200": sma200,
        "sma200_rising": sma200 > float(close.iloc[-220:-20].mean()),
        "pct_from_high": (high52 - price) / high52 if high52 else 1.0,
        "pct_above_low": (price - low52) / low52 if low52 else 0.0,
        "ret_1d": ret(1),
        "ret_1w": ret(5),
        "ret_3m": ret(63),
        "ret_6m": ret(126),
        "dollar_volume": float(df["Volume"].iloc[-50:].mean()) * price,
    }


def is_stage2(m: dict) -> bool:
    return m["price"] > m["sma50"] > m["sma200"] and m["sma200_rising"]


def structure_points(m: dict) -> float:
    """Up to 70 points for the shape of the trend."""
    score = 0.0
    score += 15 if m["price"] > m["sma50"] else 0
    score += 15 if m["sma50"] > m["sma200"] else 0
    score += 10 if m["price"] > m["sma200"] else 0
    score += 10 if m["sma200_rising"] else 0
    score += 10 if m["pct_from_high"] <= 0.25 else 0
    score += 10 if m["pct_above_low"] >= 0.30 else 0
    return score


def add_trend_scores(metrics: dict[str, dict]) -> None:
    """Set trend_score (0-100): 70 for trend structure, 30 for momentum rank within the group."""
    momentum = {t: 0.6 * m["ret_6m"] + 0.4 * m["ret_3m"] for t, m in metrics.items()}
    ordered = sorted(momentum, key=momentum.get)
    pct = {t: i / max(len(ordered) - 1, 1) for i, t in enumerate(ordered)}
    for t, m in metrics.items():
        m["trend_score"] = round(structure_points(m) + 30 * pct[t], 1)


def single_trend_score(m: dict, bench: dict) -> float:
    """trend_score for one symbol: momentum points from its 3-6m excess return over the benchmark."""
    excess = 0.6 * (m["ret_6m"] - bench["ret_6m"]) + 0.4 * (m["ret_3m"] - bench["ret_3m"])
    return round(structure_points(m) + 30 * clamp((excess + 0.15) / 0.30), 1)


def quality_score(info: dict) -> float:
    """0-100 from profitability, ROE, revenue growth and leverage (Yahoo `info` fields)."""
    margin = info.get("profitMargins")
    roe = info.get("returnOnEquity")
    growth = info.get("revenueGrowth")
    debt_to_equity = info.get("debtToEquity")  # Yahoo reports this in percent
    score = 0.0
    if margin is not None and margin > 0:
        score += 25 + 15 * clamp(margin / 0.20)
    if roe is not None:
        score += 25 * clamp(roe / 0.25)
    if growth is not None:
        score += 25 * clamp(growth / 0.20)
    if debt_to_equity is None:
        score += 5  # often missing for banks and insurers
    elif debt_to_equity < 100:
        score += 10
    elif debt_to_equity < 200:
        score += 5
    return round(score, 1)


def fetch_info(symbol: str) -> dict:
    for attempt in range(2):
        try:
            return yf.Ticker(symbol).info or {}
        except Exception as e:  # yfinance raises many types, incl. rate limits
            print(f"WARN: info {symbol}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    return {}


def rank_stocks(constituents: list[dict], histories: dict[str, pd.DataFrame]) -> tuple[dict, dict]:
    metrics = {}
    for c in constituents:
        df = histories.get(c["symbol"])
        m = trend_metrics(df) if df is not None else None
        if m and m["dollar_volume"] >= MIN_DOLLAR_VOLUME:
            metrics[c["symbol"]] = {**m, "name": c["name"], "sector": c["sector"]}
    add_trend_scores(metrics)

    ranking: dict[str, list[dict]] = {}
    stage2_count = 0
    for sector in SECTORS_PT:
        eligible = [(t, m) for t, m in metrics.items() if m["sector"] == sector and is_stage2(m)]
        stage2_count += len(eligible)
        eligible.sort(key=lambda tm: tm[1]["trend_score"], reverse=True)
        rows = []
        for symbol, m in eligible[:FUNDAMENTALS_PER_SECTOR]:
            q = quality_score(fetch_info(symbol))
            time.sleep(0.3)
            score = TREND_WEIGHT * m["trend_score"] + QUALITY_WEIGHT * q
            rows.append({"symbol": symbol, **m, "quality_score": q, "score": round(score, 1)})
        rows.sort(key=lambda r: r["score"], reverse=True)
        ranking[sector] = rows[:TOP_PER_SECTOR]
    stats = {"universe": len(constituents), "with_data": len(metrics), "stage2": stage2_count}
    return ranking, stats


def rank_etfs(histories: dict[str, pd.DataFrame]) -> tuple[list[dict], dict]:
    names = dict(UCITS_ETFS)
    metrics = {}
    for t, df in histories.items():
        m = trend_metrics(df)
        if m:
            metrics[t] = {**m, "name": names.get(t, t)}
    add_trend_scores(metrics)
    uptrend = [{"symbol": t, **m} for t, m in metrics.items() if m["price"] > m["sma200"]]
    uptrend.sort(key=lambda r: r["trend_score"], reverse=True)
    return uptrend[:TOP_ETFS], {"universe": len(UCITS_ETFS), "with_data": len(metrics)}


def pct(x: float, decimals: int = 0) -> str:
    return f"{x * 100:+.{decimals}f}%"


def returns_text(m: dict) -> str:
    """Day, week, 3-month and 6-month change, e.g. `1d +0.4% · 1s +2.1% · 3m +9% · 6m +26%`."""
    return f"1d {pct(m['ret_1d'], 1)} · 1s {pct(m['ret_1w'], 1)} · 3m {pct(m['ret_3m'])} · 6m {pct(m['ret_6m'])}"


def earnings_badge(r: dict) -> str:
    d = date.fromisoformat(r["earnings"]) if r.get("earnings") else None
    return f" · 📅 **{earnings.label(d)}**" if earnings.within(d, EARNINGS_SOON_DAYS) else ""


def stocks_post(ranking: dict, stats: dict, as_of: str) -> tuple[list[dict], dict]:
    fields = [
        (
            SECTORS_PT[sector],
            "\n".join(f"**{r['symbol']}** {r['score']:.0f} · {returns_text(r)}{earnings_badge(r)}" for r in rows)
            or "nenhuma em tendência de alta",
        )
        for sector, rows in ranking.items()
    ]
    card = embed(
        f"🏆 Top 3 por setor: tendência + qualidade (S&P 500, fecho de {as_of})",
        f"_{stats['with_data']} ações com dados → {stats['stage2']} em tendência de alta (preço > MM50 > MM200)._"
        f" Valorização: 1d = dia, 1s = semana. 📅 = resultados nos próximos {EARNINGS_SOON_DAYS} dias.",
        color=GREEN,
        fields=fields,
        footer="Score = 60% tendência (médias, força relativa 3-6m) + 40% qualidade (margem, ROE, crescimento, dívida). "
        + DISCLAIMER,
    )
    return [card], {}


def etfs_post(etfs: list[dict], stats: dict, as_of: str) -> tuple[list[dict], dict]:
    lines = [
        f"{i}. **{r['symbol'].split('.')[0]}** {r['name']} · score {r['trend_score']:.0f}\n"
        f"   {returns_text(r)}"
        for i, r in enumerate(etfs, 1)
    ] or ["Nenhum ETF acima da média de 200 dias."]
    lines += ["", f"_{stats['with_data']} de {stats['universe']} ETFs com dados._"]
    files = {}
    if etfs:
        files["etfs.png"] = returns_chart(
            [(r["symbol"].split(".")[0], r["ret_6m"] * 100) for r in etfs], "Valorização a 6 meses do top 10"
        )
    card = embed(
        f"📦 Top {TOP_ETFS} ETFs UCITS: tendência (compráveis na Europa, fecho de {as_of})",
        "\n".join(lines),
        color=BLUE,
        image="etfs.png" if files else None,
        footer="Score = estrutura de tendência (médias 50/200, máximos de 52 semanas) + força relativa 3-6m dentro da lista. "
        + DISCLAIMER,
    )
    return [card], files


def to_json(rows):
    return json.loads(json.dumps(rows, default=float))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print the messages instead of posting")
    args = parser.parse_args()

    constituents = load_constituents()
    print(f"{len(constituents)} constituintes do S&P 500", file=sys.stderr)
    stock_hist = download_history([c["symbol"] for c in constituents] + [BENCHMARK])
    etf_hist = download_history([t for t, _ in UCITS_ETFS])
    if BENCHMARK not in stock_hist:
        print("Sem dados do Yahoo Finance; nada enviado.", file=sys.stderr)
        return 1
    as_of = stock_hist[BENCHMARK].index[-1].date().isoformat()

    ranking, stock_stats = rank_stocks(constituents, stock_hist)
    dates = earnings.next_earnings_many([r["symbol"] for rows in ranking.values() for r in rows])
    for rows in ranking.values():
        for r in rows:
            r["earnings"] = dates[r["symbol"]].isoformat() if dates.get(r["symbol"]) else None
    etfs, etf_stats = rank_etfs(etf_hist)
    missing = sorted({t for t, _ in UCITS_ETFS} - set(etf_hist))
    if missing:
        print(f"WARN: sem dados para os ETFs {', '.join(missing)}", file=sys.stderr)

    WATCHLIST_DIR.mkdir(parents=True, exist_ok=True)
    (WATCHLIST_DIR / "rankings-latest.json").write_text(
        json.dumps({"as_of": as_of, "stocks": to_json(ranking), "etfs": to_json(etfs)}, indent=1)
    )

    status = send(*stocks_post(ranking, stock_stats, as_of), dry_run=args.dry_run)
    return status or send(*etfs_post(etfs, etf_stats, as_of), dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
