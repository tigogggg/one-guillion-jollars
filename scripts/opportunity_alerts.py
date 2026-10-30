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
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

import discord_bot
import earnings
from charts import price_chart
from discord_daily import REPO, WATCHLIST_DIR
from discord_post import DISCLAIMER, GREEN, embed, post_message
from rankings import SECTORS_PT, download_history

ALERTS_DIR = REPO / "state" / "alerts"
# Every alert sent, kept across days for the weekly summary's track record.
HISTORY_PATH = REPO / "state" / "history" / "alerts.json"
HISTORY_MAX = 300
NEW_YORK = ZoneInfo("America/New_York")
MAX_EXTENSION = 0.03
MIN_VOLUME_RATIO = 1.5
MAX_RISK = 0.10
PIVOT_LOOKBACK = 20
EARNINGS_WARN_DAYS = 5
PERSONAL_MOVE = 0.05  # /seguir: also DM on a daily move of 5% or more
ORANGE = 0xF0B232
STOP_LOOKBACK = 10


def load_watchlist() -> dict[str, dict]:
    """{symbol: {source, sector, score, pivot?, stop?}} from the morning run."""
    watch: dict[str, dict] = {}
    rankings = WATCHLIST_DIR / "rankings-latest.json"
    if rankings.exists():
        for sector, rows in json.loads(rankings.read_text())["stocks"].items():
            for r in rows:
                watch[r["symbol"]] = {"source": "ranking", "sector": SECTORS_PT.get(sector, sector), "score": r["score"],
                                      "earnings": r.get("earnings")}
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


def agent_holders() -> dict[str, list[str]]:
    """{symbol: ["🚀 Foguetão", …]} for agents holding it or with a buy order for the next open."""
    config, state = REPO / "agents" / "config.json", REPO / "agents" / "state.json"
    if not state.exists():
        return {}
    names = {a["id"]: f"{a['emoji']} {a['name']}" for a in json.loads(config.read_text())["agents"] if a.get("active", True)}
    out: dict[str, list[str]] = {}
    for agent_id, port in json.loads(state.read_text())["agents"].items():
        symbols = set(port["positions"]) | {o["symbol"] for o in port["pending"] if o["side"] == "buy"}
        for sym in symbols:
            if agent_id in names:
                out.setdefault(sym, []).append(names[agent_id])
    return out


def alert_card(symbol: str, info: dict, a: dict, df: pd.DataFrame, holders: list[str] = ()) -> tuple[dict, str, bytes]:
    report = date.fromisoformat(info["earnings"]) if info.get("earnings") else earnings.next_earnings(symbol)
    risky = earnings.within(report, EARNINGS_WARN_DAYS)
    origin = f"setup VCP · {info['sector']}" if info["source"] == "VCP" else f"{info['sector']} · score {info['score']:.0f}"
    description = "\n".join(
        [
            f"**${a['price']:.2f}** rompeu o pivot ${a['pivot']:.2f} ({(a['price'] / a['pivot'] - 1) * 100:+.1f}%)",
            f"Volume projetado **{a['volume_ratio']:.1f}×** a média de 50 dias",
            f"Stop sugerido **${a['stop']:.2f}** (risco {a['risk'] * 100:.1f}%)",
            f"_{origin}_",
            *([f"🤖 Na carteira (ou a comprar) de: {', '.join(holders)}"] if holders else []),
            *([f"⚠️ **Atenção: {earnings.warning(report)}.** Um rompimento antes dos resultados pode desfazer-se num dia."]
              if risky else []),
            "",
            f"Tamanho da posição: `/posicao capital:… entrada:{a['price']:.2f} stop:{a['stop']:.2f}`",
        ]
    )
    filename = f"{symbol.lower()}.png"
    chart = price_chart(df, f"{symbol}: rompimento", pivot=a["pivot"], stop=a["stop"])
    card = embed(
        f"🚨 {symbol}: possível oportunidade, rompimento com volume",
        description,
        color=ORANGE if risky else GREEN,
        image=filename,
        footer="Confirma o gráfico e o tamanho da posição antes de entrar. " + DISCLAIMER,
    )
    return card, filename, chart


def personal_card(symbol: str, df: pd.DataFrame, breakout: dict | None, move: float) -> tuple[dict, str, bytes]:
    price = float(df["Close"].iloc[-1])
    lines = [f"**${price:.2f}** · {move * 100:+.1f}% hoje"]
    if breakout:
        lines.append(f"Rompeu o pivot ${breakout['pivot']:.2f} com volume {breakout['volume_ratio']:.1f}× a média"
                     f" · stop sugerido ${breakout['stop']:.2f} (risco {breakout['risk'] * 100:.1f}%)")
    report = earnings.next_earnings(symbol)
    if earnings.within(report, EARNINGS_WARN_DAYS):
        lines.append(f"⚠️ {earnings.warning(report)}")
    lines.append("_Da tua lista `/seguir`. Para deixar de receber: `/seguir remover`._")
    what = "rompimento com volume" if breakout else ("subida forte" if move > 0 else "queda forte")
    filename = f"seguir-{symbol.lower()}.png"
    chart = price_chart(df, f"{symbol}: {what}", pivot=breakout["pivot"] if breakout else None,
                        stop=breakout["stop"] if breakout else None)
    card = embed(f"🔔 {symbol}: {what}", "\n".join(lines), color=GREEN if move >= 0 else ORANGE, image=filename)
    return card, filename, chart


def personal_alerts(now: datetime, fraction: float, dry_run: bool) -> None:
    """DM each /seguir user about their stocks breaking out or moving PERSONAL_MOVE or more today."""
    follows = discord_bot.load_follows()
    if not follows:
        return
    symbols = sorted({s for syms in follows.values() for s in syms})
    histories = download_history(symbols, period="2y")
    done_path = ALERTS_DIR / f"{now.date().isoformat()}-seguir.json"
    done = json.loads(done_path.read_text()) if done_path.exists() else {}
    for user, syms in follows.items():
        cards, files = [], {}
        for symbol in syms:
            df = histories.get(symbol)
            if df is None or len(df) < 60 or symbol in done.get(user, []) or df.index[-1].date() != now.date():
                continue
            move = float(df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1)
            breakout = check_breakout(df, {}, fraction)
            if (breakout or abs(move) >= PERSONAL_MOVE) and len(cards) < 10:
                card, filename, chart = personal_card(symbol, df, breakout, move)
                cards.append(card)
                files[filename] = chart
        if cards and discord_bot.send_dm(user, cards, files, dry_run=dry_run):
            done.setdefault(user, []).extend(c["title"].split()[1].rstrip(":") for c in cards)
    print(f"/seguir: {len(follows)} listas verificadas", file=sys.stderr)
    if not dry_run and done:
        ALERTS_DIR.mkdir(parents=True, exist_ok=True)
        done_path.write_text(json.dumps(done))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print the message instead of posting")
    args = parser.parse_args()

    now = datetime.now(NEW_YORK)
    fraction = session_fraction(now)
    personal_alerts(now, fraction, args.dry_run)

    watch = load_watchlist()
    if not watch:
        print("Sem watchlist de hoje (a execução da manhã correu?); nada a verificar.", file=sys.stderr)
        return 0

    histories = download_history(sorted(watch), period="2y")
    alerted_path = ALERTS_DIR / f"{now.date().isoformat()}.json"
    alerted = set(json.loads(alerted_path.read_text())) if alerted_path.exists() else set()

    cards, files, sent = [], {}, []
    holders = agent_holders()
    for symbol, info in watch.items():
        df = histories.get(symbol)
        if df is None or symbol in alerted or df.index[-1].date() != now.date():
            continue  # no data, already alerted, or no bar for today (market closed)
        a = check_breakout(df, info, fraction)
        if a and len(cards) < 10:  # Discord allows 10 embeds per message
            card, filename, chart = alert_card(symbol, info, a, df, holders.get(symbol, []))
            cards.append(card)
            files[filename] = chart
            alerted.add(symbol)
            sent.append(
                {"date": now.date().isoformat(), "symbol": symbol, "sector": info["sector"],
                 "price": round(a["price"], 2), "pivot": round(a["pivot"], 2), "stop": round(a["stop"], 2)}
            )

    print(f"{len(watch)} ações na watchlist, {len(cards)} alertas novos", file=sys.stderr)
    if not cards:
        return 0

    message = post_message(cards, files, dry_run=args.dry_run)
    if message is None:
        return 1
    if not args.dry_run:
        discord_bot.add_votes(message)  # 👍/👎, counted in the weekly summary
        for entry in sent:
            entry.update(message_id=message.get("id"), channel_id=message.get("channel_id"))
        ALERTS_DIR.mkdir(parents=True, exist_ok=True)
        alerted_path.write_text(json.dumps(sorted(alerted)))
        history = json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else []
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(json.dumps((history + sent)[-HISTORY_MAX:], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
