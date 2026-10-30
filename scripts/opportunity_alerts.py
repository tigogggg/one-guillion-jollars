"""Intraday breakout alerts for today's watchlist, via yfinance.

The watchlist is what the morning run saved to state/watchlist/: the top
stocks per sector (rankings.py) and the VCP setups (discord_daily.py). A stock
alerts when it trades above its pivot, at most 3% extended, on volume
projected at 1.5x its 50-day average. Each symbol alerts once per day
(state/alerts/). Posts nothing when there is nothing new.

    uv run python scripts/opportunity_alerts.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from charts import price_chart
from discord_daily import REPO, WATCHLIST_DIR
from discord_post import DISCLAIMER, GREEN, embed, send
from rankings import SECTORS_PT, download_history

ALERTS_DIR = REPO / "state" / "alerts"
NEW_YORK = ZoneInfo("America/New_York")
MAX_EXTENSION = 0.03
MIN_VOLUME_RATIO = 1.5
MAX_RISK = 0.10
PIVOT_LOOKBACK = 20
STOP_LOOKBACK = 10


def load_watchlist() -> dict[str, dict]:
    """{symbol: {source, sector, score, pivot?, stop?}} from the morning run."""
    watch: dict[str, dict] = {}
    rankings = WATCHLIST_DIR / "rankings-latest.json"
    if rankings.exists():
        for sector, rows in json.loads(rankings.read_text())["stocks"].items():
            for r in rows:
                watch[r["symbol"]] = {"source": "ranking", "sector": SECTORS_PT.get(sector, sector), "score": r["score"]}
    vcp = WATCHLIST_DIR / "vcp-latest.json"
    if vcp.exists():
        for r in json.loads(vcp.read_text()).get("results", []):
            if r.get("rating") not in ("Textbook VCP", "Strong VCP", "Good VCP"):
                continue
            piv = r.get("pivot_proximity", {})
            entry = watch.setdefault(r["symbol"], {"sector": r.get("sector", "")})
            entry.update(source="VCP", pivot=piv.get("pivot_price"), stop=piv.get("stop_loss_price"))
    return watch


def session_fraction(now: datetime) -> float:
    """Share of the regular NYSE session (09:30-16:00 ET) elapsed at `now`."""
    ny = now.astimezone(NEW_YORK)
    opened = datetime.combine(ny.date(), time(9, 30), NEW_YORK)
    elapsed = (ny - opened).total_seconds() / (6.5 * 3600)
    return max(0.05, min(1.0, elapsed))


def check_breakout(df: pd.DataFrame, info: dict, fraction: float) -> dict | None:
    """Return alert details if today's bar (last row) breaks out, else None."""
    if len(df) < 60:
        return None
    today, before = df.iloc[-1], df.iloc[:-1]
    price = float(today["Close"])
    pivot = info.get("pivot") or float(before["High"].iloc[-PIVOT_LOOKBACK:].max())
    stop = info.get("stop") or float(before["Low"].iloc[-STOP_LOOKBACK:].min())
    avg_volume = float(before["Volume"].iloc[-50:].mean())
    volume_ratio = float(today["Volume"]) / fraction / avg_volume if avg_volume else 0.0
    risk = (price - stop) / price if price else 1.0

    if not pivot < price <= pivot * (1 + MAX_EXTENSION):
        return None
    if volume_ratio < MIN_VOLUME_RATIO or not 0 < risk <= MAX_RISK:
        return None
    return {"price": price, "pivot": pivot, "stop": stop, "risk": risk, "volume_ratio": volume_ratio}


def alert_card(symbol: str, info: dict, a: dict, df: pd.DataFrame) -> tuple[dict, str, bytes]:
    origin = f"setup VCP · {info['sector']}" if info["source"] == "VCP" else f"{info['sector']} · score {info['score']:.0f}"
    description = "\n".join(
        [
            f"**${a['price']:.2f}** rompeu o pivot ${a['pivot']:.2f} ({(a['price'] / a['pivot'] - 1) * 100:+.1f}%)",
            f"Volume projetado **{a['volume_ratio']:.1f}×** a média de 50 dias",
            f"Stop sugerido **${a['stop']:.2f}** (risco {a['risk'] * 100:.1f}%)",
            f"_{origin}_",
            "",
            f"Tamanho da posição: `/posicao capital:… entrada:{a['price']:.2f} stop:{a['stop']:.2f}`",
        ]
    )
    filename = f"{symbol.lower()}.png"
    chart = price_chart(df, f"{symbol}: rompimento", pivot=a["pivot"], stop=a["stop"])
    card = embed(
        f"🚨 {symbol}: possível oportunidade, rompimento com volume",
        description,
        color=GREEN,
        image=filename,
        footer="Confirma o gráfico e o tamanho da posição antes de entrar. " + DISCLAIMER,
    )
    return card, filename, chart


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print the message instead of posting")
    args = parser.parse_args()

    watch = load_watchlist()
    if not watch:
        print("Sem watchlist de hoje (a execução da manhã correu?); nada a verificar.", file=sys.stderr)
        return 0

    now = datetime.now(NEW_YORK)
    histories = download_history(sorted(watch), period="2y")
    alerted_path = ALERTS_DIR / f"{now.date().isoformat()}.json"
    alerted = set(json.loads(alerted_path.read_text())) if alerted_path.exists() else set()

    cards, files, fraction = [], {}, session_fraction(now)
    for symbol, info in watch.items():
        df = histories.get(symbol)
        if df is None or symbol in alerted or df.index[-1].date() != now.date():
            continue  # no data, already alerted, or no bar for today (market closed)
        a = check_breakout(df, info, fraction)
        if a and len(cards) < 10:  # Discord allows 10 embeds per message
            card, filename, chart = alert_card(symbol, info, a, df)
            cards.append(card)
            files[filename] = chart
            alerted.add(symbol)

    print(f"{len(watch)} ações na watchlist, {len(cards)} alertas novos", file=sys.stderr)
    if not cards:
        return 0

    status = send(cards, files, dry_run=args.dry_run)
    if status == 0 and not args.dry_run:
        ALERTS_DIR.mkdir(parents=True, exist_ok=True)
        alerted_path.write_text(json.dumps(sorted(alerted)))
    return status


if __name__ == "__main__":
    sys.exit(main())
