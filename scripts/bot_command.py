"""Answer a Discord slash command that the Cloudflare Worker handed to GitHub Actions.

Edits the bot's "a analisar…" reply with an embed and chart:
  /acao TICKER   trend + quality score, pivot and stop of a US stock
  /etf TICKER    trend of a UCITS ETF
  /mercado       market breadth now (same card as the daily tips)

Inputs come from the discord-bot.yml workflow as environment variables:
COMMAND, ARG, APPLICATION_ID, INTERACTION_TOKEN. With --dry-run the card is
printed and the chart saved to reports/preview/ instead.

    COMMAND=acao ARG=NVDA uv run python scripts/bot_command.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

from charts import price_chart
from discord_daily import BREADTH_DETAIL_URL, BREADTH_SUMMARY_URL, REPO, build_post, run_skill
from discord_post import GREEN, RED, embed, send
from rankings import (
    BENCHMARK,
    QUALITY_WEIGHT,
    TREND_WEIGHT,
    UCITS_ETFS,
    download_history,
    fetch_info,
    is_stage2,
    quality_score,
    returns_text,
    single_trend_score,
    trend_metrics,
)

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")
ORANGE = 0xF0B232
ETF_EXCHANGES = (".DE", ".L", ".AS", ".MI", ".PA", ".SW")


def check(ok: bool) -> str:
    return "✅" if ok else "❌"


def levels(df) -> tuple[float, float]:
    """Pivot (high of the 20 sessions before today) and stop (low of the last 10 before today)."""
    before = df.iloc[:-1]
    return float(before["High"].iloc[-20:].max()), float(before["Low"].iloc[-10:].min())


def trend_lines(m: dict) -> list[str]:
    return [
        f"{check(m['price'] > m['sma50'])} preço > média 50d ({m['sma50']:.2f})"
        f" · {check(m['sma50'] > m['sma200'])} média 50d > 200d ({m['sma200']:.2f})",
        f"{check(m['sma200_rising'])} média 200d a subir"
        f" · a {m['pct_from_high'] * 100:.0f}% do máximo de 52 semanas",
    ]


def colour(m: dict, score: float) -> int:
    if is_stage2(m) and score >= 70:
        return GREEN
    return ORANGE if m["price"] > m["sma200"] else RED


def stock_card(ticker: str) -> tuple[list[dict], dict]:
    hist = download_history([ticker, BENCHMARK], period="2y")
    df, bench_df = hist.get(ticker), hist.get(BENCHMARK)
    m = trend_metrics(df) if df is not None else None
    if m is None or bench_df is None:
        return [embed(f"❓ {ticker}", "Não encontrei um ano de dados para este ticker no Yahoo Finance.", color=RED)], {}

    info = fetch_info(ticker)
    trend = single_trend_score(m, trend_metrics(bench_df))
    quality = quality_score(info)
    score = TREND_WEIGHT * trend + QUALITY_WEIGHT * quality
    pivot, stop = levels(df)
    risk = (m["price"] - stop) / m["price"]
    vs_pivot = m["price"] / pivot - 1

    def pct_or_dash(key: str) -> str:
        v = info.get(key)
        return f"{v * 100:.0f}%" if v is not None else "—"

    quality_line = (
        f"Margem {pct_or_dash('profitMargins')} · ROE {pct_or_dash('returnOnEquity')}"
        f" · crescimento das receitas {pct_or_dash('revenueGrowth')}"
    )
    if info.get("debtToEquity") is not None:
        quality_line += f" · dívida/capital {info['debtToEquity'] / 100:.1f}x"

    lines = [
        f"**${m['price']:.2f}** · {returns_text(m)}",
        f"**Score {score:.0f}/100** · tendência {trend:.0f} · qualidade {quality:.0f}",
        "",
        "**Tendência**",
        *trend_lines(m),
        "",
        "**Qualidade**",
        quality_line,
        "",
        "**Níveis**",
        f"Pivot ${pivot:.2f} (máximo de 20 sessões) · "
        + (f"**acima do pivot ({vs_pivot * 100:+.1f}%)**" if vs_pivot > 0 else f"a {-vs_pivot * 100:.1f}% do pivot"),
        f"Stop ${stop:.2f} (mínimo de 10 sessões) · risco {risk * 100:.1f}%",
        f"`/posicao capital:… entrada:{m['price']:.2f} stop:{stop:.2f}`",
    ]
    name = info.get("longName") or info.get("shortName") or ticker
    sector = info.get("sector")
    card = embed(
        f"🔎 {ticker} · {name}" + (f" ({sector})" if sector else ""),
        "\n".join(lines),
        color=colour(m, score),
        image="grafico.png",
    )
    return [card], {"grafico.png": price_chart(df, f"{ticker}: 6 meses", pivot=pivot, stop=stop)}


def resolve_etf(ticker: str) -> list[str]:
    """Yahoo symbols to try for an ETF typed with or without exchange suffix."""
    if "." in ticker:
        return [ticker]
    known = [t for t, _ in UCITS_ETFS if t.split(".")[0] == ticker]
    return known or [ticker + suffix for suffix in ETF_EXCHANGES]


def etf_card(ticker: str) -> tuple[list[dict], dict]:
    candidates = resolve_etf(ticker)
    hist = download_history(candidates + [BENCHMARK], period="2y")
    symbol = next((c for c in candidates if c in hist and trend_metrics(hist[c])), None)
    if symbol is None or BENCHMARK not in hist:
        tried = ", ".join(candidates)
        return [embed(f"❓ {ticker}", f"Não encontrei um ano de dados no Yahoo Finance (tentei {tried}).", color=RED)], {}

    df = hist[symbol]
    m = trend_metrics(df)
    trend = single_trend_score(m, trend_metrics(hist[BENCHMARK]))
    names = dict(UCITS_ETFS)
    lines = [
        f"**{m['price']:.2f}** · {returns_text(m)}",
        f"**Score de tendência {trend:.0f}/100** (força relativa medida contra o S&P 500)",
        "",
        *trend_lines(m),
    ]
    card = embed(
        f"📦 {symbol} · {names.get(symbol, 'ETF')}",
        "\n".join(lines),
        color=colour(m, trend),
        image="grafico.png",
    )
    return [card], {"grafico.png": price_chart(df, f"{symbol}: 6 meses")}


def market_card() -> tuple[list[dict], dict]:
    out_dir = REPO / "reports" / f"bot-{date.today().isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    uptrend = run_skill("skills/uptrend-analyzer/scripts/uptrend_analyzer.py", out_dir)
    if uptrend is None:
        return [embed("❓ Mercado", "Não consegui obter os dados de breadth agora. Tenta mais tarde.", color=RED)], {}
    breadth = run_skill(
        "skills/market-breadth-analyzer/scripts/market_breadth_analyzer.py",
        out_dir,
        "--detail-url",
        BREADTH_DETAIL_URL,
        "--summary-url",
        BREADTH_SUMMARY_URL,
    )
    return build_post(uptrend, breadth, title="📊 Mercado agora")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print the card instead of editing the reply")
    args = parser.parse_args()

    command = os.environ.get("COMMAND", "")
    arg = os.environ.get("ARG", "").strip().upper()
    reply_url = (
        f"https://discord.com/api/v10/webhooks/{os.environ.get('APPLICATION_ID', '')}"
        f"/{os.environ.get('INTERACTION_TOKEN', '')}/messages/@original"
    )

    try:
        if command in ("acao", "etf") and not TICKER_RE.match(arg):
            embeds, files = [embed("❓ Ticker inválido", f"`{arg[:20]}` não parece um ticker (ex.: NVDA, VWCE).", color=RED)], {}
        elif command == "acao":
            embeds, files = stock_card(arg)
        elif command == "etf":
            embeds, files = etf_card(arg)
        elif command == "mercado":
            embeds, files = market_card()
        else:
            embeds, files = [embed("❓ Comando desconhecido", f"/{command}", color=RED)], {}
    except Exception as e:  # always answer, or the reply stays at "a analisar…"
        print(f"ERROR: {e!r}", file=sys.stderr)
        embeds, files = [embed("⚠️ Erro na análise", f"Algo falhou: {str(e)[:300]}", color=RED)], {}

    return send(embeds, files, dry_run=args.dry_run, url=reply_url, method="PATCH")


if __name__ == "__main__":
    sys.exit(main())
