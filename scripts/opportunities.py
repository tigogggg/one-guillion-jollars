"""Buy and sell opportunities with a 0-100 safety score, for the portal's "Oportunidades" tab.

The safety score adds eight criteria with fixed, published rules (CRITERIA);
the page shows each stock's points per criterion next to the rule. It measures
how orderly and well-supported a stock is right now, not how much it will rise.

Buy ideas: the sector rankings, the VCP setups, today's breakouts on volume and
stocks with important positive news, all in a Stage 2 uptrend. Sell / avoid:
what the agents hold and the ranking names that show warning signs (below the
50- or 200-day average, falling 200-day average, far from the high, important
negative news, results within days). Built by the agents' daily run from the
market it already loaded, and stored in the Worker's KV for the portal.
"""

from __future__ import annotations

from datetime import date

ZONE_POINTS = {"Alta": 5, "Neutra": 3, "Cautelosa": 1, "Queda": 0}

# (key, label, max points, the rule as shown on the page)
CRITERIA = [
    ("tendencia", "📈 Tendência", 25,
     "+10 se o preço está acima da média de 50 dias; +10 se a média de 50 dias está acima da de 200; "
     "+5 se a média de 200 dias está a subir (face a há um mês)."),
    ("volatilidade", "🌊 Volatilidade", 20,
     "Desvio-padrão das variações diárias das últimas 20 sessões: 1,5% ou menos = 20 pontos; 4% ou mais = 0; "
     "proporcional entre os dois."),
    ("maximo", "🏔️ Distância ao máximo de 52 semanas", 15,
     "5% ou menos abaixo do máximo = 15 pontos; 30% ou mais abaixo = 0; proporcional entre os dois."),
    ("liquidez", "💧 Liquidez", 10,
     "Volume médio diário em dólares (50 sessões): 1 000 M$ ou mais = 10; 300 M$ = 7; 100 M$ = 4; menos = 1."),
    ("qualidade", "💎 Qualidade da empresa", 15,
     "Score de qualidade do ranking (margem de lucro, rentabilidade dos capitais, crescimento das vendas e "
     "dívida, 0-100) × 0,15. Sem dados = 7. Nos ETFs, 15 (carteira diversificada)."),
    ("resultados", "📅 Resultados", 5,
     "Sem apresentação de resultados nos próximos 10 dias = 5; com resultados = 0 (o preço pode saltar). "
     "Data desconhecida = 3. Nos ETFs, 5."),
    ("noticias", "📰 Notícias (36 h)", 5,
     "Score das notícias lidas pelo Claude (sentimento × importância): positivo = 5; sem notícias ou neutro = 4; "
     "ligeiramente negativo = 2; -3 ou pior = 0."),
    ("mercado", "🧭 Estado do mercado", 5,
     "Zona do mercado pela breadth do S&P 500: Alta = 5; Neutra = 3; Cautelosa = 1; Queda = 0. "
     "Igual para todas as ações do dia."),
]
LEVELS = [(75, "Alta"), (55, "Média"), (35, "Baixa"), (0, "Muito baixa")]
MAX_BUY = 30
MAX_SELL = 30


def lerp(x: float, best: float, worst: float, points: float) -> float:
    """points at `best` or better, 0 at `worst` or worse, straight line between."""
    if best < worst:
        return points if x <= best else 0.0 if x >= worst else points * (worst - x) / (worst - best)
    return points if x >= best else 0.0 if x <= worst else points * (x - worst) / (best - worst)


def safety(m: dict, quality: float | None, earnings_days: int | None, news_score: float | None, zone: str,
           etf: bool = False) -> tuple[int, list[dict]]:
    """(score 0-100, [{key, points, max, why}]) for one stock's metrics."""
    trend = (10 if m["price"] > m["sma50"] else 0) + (10 if m["sma50"] > m["sma200"] else 0) + (5 if m["sma200_rising"] else 0)
    vol = m.get("vol20")
    dv = m.get("dollar_volume", 0) or 0
    parts = {
        "tendencia": (trend, ("acima" if m["price"] > m["sma50"] else "abaixo") + " da média de 50 dias, média de 50 "
                      + ("acima" if m["sma50"] > m["sma200"] else "abaixo") + " da de 200, média de 200 "
                      + ("a subir" if m["sma200_rising"] else "a descer")),
        "volatilidade": (lerp(vol, 0.015, 0.04, 20) if vol is not None else 10,
                         f"{vol * 100:.1f}% por dia" if vol is not None else "sem dados"),
        "maximo": (lerp(m["pct_from_high"], 0.05, 0.30, 15), f"{m['pct_from_high'] * 100:.0f}% abaixo do máximo"),
        "liquidez": ((10 if dv >= 1e9 else 7 if dv >= 3e8 else 4 if dv >= 1e8 else 1) if not etf else 10,
                     f"{dv / 1e6:,.0f} M$ por dia".replace(",", " ") if not etf else "ETF UCITS"),
        "qualidade": (15 if etf else 7 if quality is None else quality * 0.15,
                      "ETF" if etf else "sem dados" if quality is None else f"score {quality:.0f}/100"),
        "resultados": (5 if etf else 3 if earnings_days is None else 0 if earnings_days <= 10 else 5,
                       "ETF" if etf else "data desconhecida" if earnings_days is None
                       else f"resultados daqui a {earnings_days} dias" if earnings_days <= 10 else "sem resultados nos próximos 10 dias"),
        "noticias": (4 if not news_score else 5 if news_score > 0 else 2 if news_score > -3 else 0,
                     "sem notícias relevantes" if not news_score else f"score {news_score:+.0f}"),
        "mercado": (ZONE_POINTS.get(zone, 3), f"zona {zone}"),
    }
    breakdown = [{"key": k, "label": label, "points": round(parts[k][0], 1), "max": mx, "why": parts[k][1]}
                 for k, label, mx, _ in CRITERIA]
    return round(sum(b["points"] for b in breakdown)), breakdown


def level(score: int) -> str:
    return next(name for floor, name in LEVELS if score >= floor)


def build(mkt, holders: dict[str, list[str]], fetch_earnings=None) -> dict:
    """{"buy": [...], "sell": [...], "criteria": [...], ...} from the agents' Market and who holds what.

    fetch_earnings(symbols) -> {symbol: date | None} fills in results dates not looked up yet.
    """
    today = mkt.as_of
    quality = {r["symbol"]: r.get("quality_score") for r in mkt.rankings}
    ranked = {r["symbol"] for r in mkt.rankings}

    def days_to(sym: str) -> int | None:
        d = mkt.earnings.get(sym)
        return (d - today).days if isinstance(d, date) and d >= today else None

    def news_of(sym: str) -> tuple[float | None, str]:
        sig = mkt.news.get(sym) or {}
        items = sorted(sig.get("items", []), key=lambda i: (i.get("importancia", 0), i.get("published", "")), reverse=True)
        return sig.get("score"), (items[0].get("resumo") or items[0].get("title", "")) if items else ""

    def row(sym: str, m: dict, etf: bool = False) -> dict:
        news_score, headline = news_of(sym)
        score, parts = safety(m, quality.get(sym), days_to(sym), news_score, mkt.zone, etf)
        stop = min(m.get("low10_prev", m["price"] * 0.92), m["price"] * 0.97) if not etf else m["price"] * 0.92
        return {
            "symbol": sym, "name": m.get("name", ""), "sector": m.get("sector", ""), "etf": etf,
            "price": round(m["price"], 2), "ret_1d": round(m["ret_1d"] * 100, 2), "ret_1w": round(m["ret_1w"] * 100, 2),
            "ret_3m": round(m["ret_3m"] * 100, 1), "trend_score": round(m.get("trend_score", 0)),
            "safety": score, "level": level(score), "parts": parts,
            "stop": round(stop, 2), "risk_pct": round((m["price"] - stop) / m["price"] * 100, 1),
            "earnings": mkt.earnings[sym].isoformat() if isinstance(mkt.earnings.get(sym), date) else None,
            "news": headline, "held_by": holders.get(sym, []),
        }

    # First pass: which stocks qualify, and why.
    buy_reasons: dict[str, list[str]] = {}
    for sym, m in mkt.stocks.items():
        reasons = []
        if sym in ranked:
            reasons.append("🏆 top 3 do setor no ranking")
        if sym in mkt.vcp:
            reasons.append("🔍 setup VCP perto do pivot")
        if m.get("stage2") and m["price"] > m.get("high20_prev", float("inf")) and m.get("vol_ratio", 0) >= 1.5:
            reasons.append(f"🚀 rompeu o máximo de 20 dias com {m['vol_ratio']:.1f}× o volume")
        news_score, _ = news_of(sym)
        if news_score and news_score >= 3:
            reasons.append("📰 notícias importantes e positivas")
        if reasons and m.get("stage2"):
            buy_reasons[sym] = reasons
    watch = [s for s in sorted(set(holders) | ranked) if s in mkt.stocks or s in mkt.etfs]

    # Results dates for the stocks shown, once (one Yahoo call each).
    missing = [s for s in set(buy_reasons) | set(watch) if s in mkt.stocks and s not in mkt.earnings]
    if missing and fetch_earnings:
        mkt.earnings.update(fetch_earnings(missing))

    buy = [{**row(sym, mkt.stocks[sym]), "reasons": reasons} for sym, reasons in buy_reasons.items()]
    for sym, m in sorted(mkt.etfs.items(), key=lambda kv: kv[1].get("trend_score", 0), reverse=True)[:5]:
        if m["price"] > m["sma50"] > m["sma200"]:
            buy.append({**row(sym, m, etf=True), "reasons": ["📦 um dos 5 ETFs UCITS com melhor tendência"]})
    buy.sort(key=lambda r: (r["safety"], r["trend_score"]), reverse=True)

    sell = []
    for sym in watch:
        m = mkt.stocks.get(sym) or mkt.etfs.get(sym)
        warnings = []
        if m["price"] < m["sma50"]:
            warnings.append("📉 abaixo da média de 50 dias")
        if m["price"] < m["sma200"]:
            warnings.append("🔻 abaixo da média de 200 dias (tendência de fundo perdida)")
        if not m["sma200_rising"]:
            warnings.append("↘️ média de 200 dias a descer")
        if m["pct_from_high"] > 0.20:
            warnings.append(f"🏔️ {m['pct_from_high'] * 100:.0f}% abaixo do máximo de 52 semanas")
        news_score, _ = news_of(sym)
        if news_score is not None and news_score <= -3:
            warnings.append("📰 notícias importantes e negativas")
        d = days_to(sym)
        if d is not None and d <= 5:
            warnings.append(f"📅 resultados daqui a {d} dias")
        if not warnings:
            continue
        r = row(sym, m, etf=sym in mkt.etfs)
        sell.append({**r, "warnings": warnings, "severity": len(warnings) + (2 if m["price"] < m["sma200"] else 0)})
    sell.sort(key=lambda r: (r["severity"], -r["safety"]), reverse=True)

    return {
        "as_of": today.isoformat(), "zone": mkt.zone, "stage2_pct": round(mkt.stage2_share * 100, 1),
        "buy": buy[:MAX_BUY], "sell": sell[:MAX_SELL],
        "criteria": [{"key": k, "label": label, "max": mx, "rule": rule} for k, label, mx, rule in CRITERIA],
        "levels": [{"from": f, "name": n} for f, n in LEVELS],
    }
