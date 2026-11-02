"""Buy and sell opportunities with a 0-100 safety score, for the portal's "Oportunidades" tab.

The safety score adds eight criteria with fixed, published rules (CRITERIA); the page shows each stock's
points per criterion next to the rule. It measures how orderly and well-supported a stock is right now,
not how much it will rise.

Every stock lands in at most one list, in this order of precedence:

1. 🔴 Vender (SELL_RULES): the trend broke. Below the 200-day average, below the 50-day average with the
   200-day falling or 15% off the high, or 25% or more below the 52-week high. For what the agents hold
   and the ranking names.
2. 🟡 Atenção (WATCH_RULES): a strong stock with a reason not to buy now: results within 7 days,
   important negative news, lost the 50-day average inside an intact uptrend, or stretched more than 20%
   above the 50-day average.
3. 🟢 Comprar (BUY_RULES): every rule must hold: Stage 2 trend, an entry signal (VCP near the pivot,
   breakout on volume close to the pivot, or a ranking leader back near its 50-day average), not
   stretched, no results within 10 days, no negative news, enough liquidity, stop within 8% and a
   safety score of at least 70 (80 in a cautious market; none in a falling one). The best MAX_BUY.

Built by the agents' daily run from the market it already loaded, and stored in the Worker's KV.
"""

from __future__ import annotations

from datetime import date

ZONE_POINTS = {"Alta": 3, "Neutra": 2, "Cautelosa": 1, "Queda": 0}

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
    ("qualidade", "💎 Fundamentos", 15,
     "Relatórios da SEC: qualidade (margens, crescimento das receitas, cash flow livre, liquidez e dívida, 0-100) "
     "× 0,10 + valor (P/E, rendimento do cash flow livre e margem de segurança face ao valor intrínseco, 0-100) × 0,05. "
     "Sem relatórios: o score de qualidade do Yahoo × 0,15; sem dados = 7. Nos ETFs, 15 (carteira diversificada)."),
    ("resultados", "📅 Resultados", 5,
     "Sem apresentação de resultados nos próximos 10 dias = 5; com resultados = 0 (o preço pode saltar). "
     "Data desconhecida = 3. Nos ETFs, 5."),
    ("noticias", "📰 Notícias (36 h)", 5,
     "Score das notícias lidas pelo Claude (sentimento × importância): positivo = 5; sem notícias ou neutro = 4; "
     "ligeiramente negativo = 2; -3 ou pior = 0."),
    ("mercado", "🧭 Mercado e setor", 5,
     "Zona do mercado pela breadth do S&P 500: Alta = 3; Neutra = 2; Cautelosa = 1; Queda = 0. "
     "Mais 2 se o ETF do setor da ação estiver acima da média de 200 dias (1 se o setor for desconhecido)."),
]
LEVELS = [(75, "Alta"), (55, "Média"), (35, "Baixa"), (0, "Muito baixa")]
MAX_BUY = 10
MAX_SELL = 25
MAX_WATCH = 25
MIN_BUY_SAFETY = {"Alta": 70, "Neutra": 70, "Cautelosa": 80}  # no stock buys in "Queda"
MAX_STRETCH = 0.12  # buy at most 12% above the 50-day average
MAX_ABOVE_PIVOT = 0.05  # a breakout is still buyable up to 5% above the pivot
PULLBACK = 0.04  # "back near the 50-day average": within 4% above it
MAX_RISK = 0.08  # stop at most 8% below
MIN_DOLLAR_VOLUME = 1e8
EARNINGS_BUY_DAYS = 10
EARNINGS_WATCH_DAYS = 7
NEWS_BAD = -3
MIN_FUND_Q = 40  # SEC fundamentals quality, when known

BUY_RULES = [
    "Tendência de alta (Stage 2): preço acima da média de 50 dias, esta acima da de 200, e a de 200 a subir.",
    "Um sinal de entrada: setup VCP perto do pivot; rompimento do máximo de 20 dias com 1,5× o volume e no máximo 5% acima dele; "
    "ou ação do top 3 do setor que voltou para perto da média de 50 dias (até 4% acima).",
    "Não esticada: no máximo 12% acima da média de 50 dias.",
    "Sem resultados nos próximos 10 dias e sem notícias negativas nas últimas 36 h.",
    "Liquidez de pelo menos 100 M$ por dia e stop no máximo 8% abaixo.",
    "Setor saudável: o ETF do setor acima da média de 200 dias; e fundamentos da SEC com qualidade de pelo menos 40/100 (quando há relatórios).",
    "Score de segurança de 70 ou mais (80 com o mercado em zona Cautelosa; nenhuma ação com o mercado em Queda).",
    "Uma ação só aparece numa lista: se tiver sinais de venda ou de atenção, não é proposta para compra.",
]
SELL_RULES = [
    "Abaixo da média de 200 dias (tendência de fundo perdida).",
    "Abaixo da média de 50 dias com a de 200 a descer, ou a mais de 15% do máximo de 52 semanas.",
    "25% ou mais abaixo do máximo de 52 semanas.",
]
WATCH_RULES = [
    "Resultados nos próximos 7 dias (o preço pode saltar para qualquer lado).",
    "Notícias importantes e negativas nas últimas 36 h.",
    "Perdeu a média de 50 dias, mas a tendência de fundo continua de alta.",
    "Esticada: mais de 20% acima da média de 50 dias (não perseguir).",
]


def lerp(x: float, best: float, worst: float, points: float) -> float:
    """points at `best` or better, 0 at `worst` or worse, straight line between."""
    if best < worst:
        return points if x <= best else 0.0 if x >= worst else points * (worst - x) / (worst - best)
    return points if x >= best else 0.0 if x <= worst else points * (x - worst) / (best - worst)


def safety(m: dict, quality: float | None, earnings_days: int | None, news_score: float | None, zone: str,
           etf: bool = False, sector_trend: float | None = None) -> tuple[int, list[dict]]:
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
        "qualidade": fundamentals_points(m, quality, etf),
        "resultados": (5 if etf else 3 if earnings_days is None else 0 if earnings_days <= 10 else 5,
                       "ETF" if etf else "data desconhecida" if earnings_days is None
                       else f"resultados daqui a {earnings_days} dias" if earnings_days <= 10 else "sem resultados nos próximos 10 dias"),
        "noticias": (4 if not news_score else 5 if news_score > 0 else 2 if news_score > -3 else 0,
                     "sem notícias relevantes" if not news_score else f"score {news_score:+.0f}"),
        "mercado": (ZONE_POINTS.get(zone, 2) + (1 if sector_trend is None else 2 if sector_trend >= 0 else 0),
                    f"zona {zone}" + ("" if sector_trend is None else f"; setor {sector_trend * 100:+.0f}% face à média de 200 dias")),
    }
    breakdown = [{"key": k, "label": label, "points": round(parts[k][0], 1), "max": mx, "why": parts[k][1]}
                 for k, label, mx, _ in CRITERIA]
    return round(sum(b["points"] for b in breakdown)), breakdown


def fundamentals_points(m: dict, quality: float | None, etf: bool) -> tuple[float, str]:
    """(points 0-15, why) from the SEC fundamentals, else the Yahoo quality score."""
    if etf:
        return 15, "ETF"
    q, v = m.get("fund_q"), m.get("fund_v")
    if q is not None:
        bits = [f"qualidade {q:.0f}/100"]
        if v is not None:
            bits.append(f"valor {v:.0f}/100")
        if m.get("pe"):
            bits.append(f"P/E {m['pe']:.0f}")
        if m.get("mos") is not None:
            bits.append(f"margem de segurança {m['mos'] * 100:+.0f}%")
        return q * 0.10 + (v if v is not None else 50) * 0.05, ", ".join(bits)
    if quality is not None:
        return quality * 0.15, f"score Yahoo {quality:.0f}/100"
    return 7, "sem dados"


def level(score: int) -> str:
    return next(name for floor, name in LEVELS if score >= floor)


def flags(m: dict, earnings_days: int | None, news_score: float | None) -> tuple[list[str], list[str]]:
    """(sell signals, attention signals) for one stock, by SELL_RULES and WATCH_RULES."""
    sell, watch = [], []
    below50, below200 = m["price"] < m["sma50"], m["price"] < m["sma200"]
    off_high = m["pct_from_high"]
    if below200:
        sell.append("🔻 abaixo da média de 200 dias (tendência de fundo perdida)")
    if below50 and not below200 and (not m["sma200_rising"] or off_high > 0.15):
        sell.append("📉 abaixo da média de 50 dias" + (" com a de 200 a descer" if not m["sma200_rising"] else f" e {off_high * 100:.0f}% abaixo do máximo"))
    if off_high >= 0.25:
        sell.append(f"🏔️ {off_high * 100:.0f}% abaixo do máximo de 52 semanas")
    if earnings_days is not None and earnings_days <= EARNINGS_WATCH_DAYS:
        watch.append(f"📅 resultados daqui a {earnings_days} dias")
    if news_score is not None and news_score <= NEWS_BAD:
        watch.append("📰 notícias importantes e negativas")
    if below50 and not sell:
        watch.append("📉 perdeu a média de 50 dias (a tendência de fundo continua de alta)")
    stretch = m["price"] / m["sma50"] - 1 if m["sma50"] else 0
    if stretch > 0.20:
        watch.append(f"🎢 esticada: {stretch * 100:.0f}% acima da média de 50 dias (não perseguir)")
    return sell, watch


def build(mkt, holders: dict[str, list[str]], fetch_earnings=None) -> dict:
    """{"buy", "watch", "sell", "criteria", "rules", ...} from the agents' Market and who holds what.

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

    def stop_of(m: dict, etf: bool) -> float:
        if etf:
            return m["price"] * 0.92
        atr = m.get("atr14") or 0
        base = m.get("low10_prev", m["price"] * 0.92)
        return min(base, m["price"] - 1.5 * atr) if atr else min(base, m["price"] * 0.97)

    def sector_trend(m: dict) -> float | None:
        import context

        key = context.sector_key(m.get("sector") or "")
        return (getattr(mkt, "context", {}) or {}).get(f"{key}_trend") if key else None

    def row(sym: str, m: dict, etf: bool = False) -> dict:
        news_score, headline = news_of(sym)
        score, parts = safety(m, quality.get(sym), days_to(sym), news_score, mkt.zone, etf, None if etf else sector_trend(m))
        stop = stop_of(m, etf)
        return {
            "symbol": sym, "name": m.get("name", ""), "sector": m.get("sector", ""), "etf": etf,
            "currency": "EUR" if etf else m.get("currency", "USD"),
            "price": round(m["price"], 2), "ret_1d": round(m["ret_1d"] * 100, 2), "ret_1w": round(m["ret_1w"] * 100, 2),
            "ret_3m": round(m["ret_3m"] * 100, 1), "trend_score": round(m.get("trend_score", 0)),
            "safety": score, "level": level(score), "parts": parts,
            "stop": round(stop, 2), "risk_pct": round((m["price"] - stop) / m["price"] * 100, 1),
            "stretch_pct": round((m["price"] / m["sma50"] - 1) * 100, 1) if m["sma50"] else None,
            "earnings": mkt.earnings[sym].isoformat() if isinstance(mkt.earnings.get(sym), date) else None,
            "news": headline, "held_by": holders.get(sym, []),
            "fund": {k: round(m[k], 3) for k in ("fund_q", "fund_v", "mos", "pe", "fcf_yield", "rev_growth", "op_margin", "current_ratio")
                     if m.get(k) is not None} if not etf else {},
            "sector_trend": None if etf or sector_trend(m) is None else round(sector_trend(m) * 100, 1),
        }

    def stage2(m: dict) -> bool:
        return bool(m.get("stage2")) and m["price"] > m["sma50"] > m["sma200"] and m["sma200_rising"]

    def entry_signals(sym: str, m: dict) -> list[str]:
        out = []
        pivot = m.get("high20_prev")
        if sym in mkt.vcp:
            out.append("🔍 setup VCP perto do pivot")
        if pivot and m["price"] > pivot and m.get("vol_ratio", 0) >= 1.5 and m["price"] <= pivot * (1 + MAX_ABOVE_PIVOT):
            out.append(f"🚀 rompeu o máximo de 20 dias com {m['vol_ratio']:.1f}× o volume")
        if sym in ranked and 0 <= m["price"] / m["sma50"] - 1 <= PULLBACK:
            out.append("🎯 líder do setor de volta à média de 50 dias")
        return out

    # Candidates for each list; earnings dates looked up once for all of them.
    watchlist = sorted(s for s in set(holders) | ranked if s in mkt.stocks or s in mkt.etfs)
    candidates = [s for s, m in mkt.stocks.items() if stage2(m) and entry_signals(s, m)]
    missing = [s for s in set(candidates) | set(watchlist) if s in mkt.stocks and s not in mkt.earnings]
    if missing and fetch_earnings:
        mkt.earnings.update(fetch_earnings(missing))

    sell, watch, flagged = [], [], set()
    for sym in sorted(set(watchlist) | set(candidates)):
        m = mkt.stocks.get(sym) or mkt.etfs.get(sym)
        s_sig, w_sig = flags(m, days_to(sym), news_of(sym)[0])
        if s_sig and sym in watchlist:
            sell.append({**row(sym, m, etf=sym in mkt.etfs), "warnings": s_sig + w_sig,
                         "severity": len(s_sig) * 2 + len(w_sig) + (2 if m["price"] < m["sma200"] else 0)})
            flagged.add(sym)
        elif s_sig or w_sig:
            if sym in watchlist or sym in candidates:
                watch.append({**row(sym, m, etf=sym in mkt.etfs), "warnings": s_sig + w_sig, "severity": len(s_sig) * 2 + len(w_sig)})
            flagged.add(sym)
    sell.sort(key=lambda r: (r["severity"], -r["safety"]), reverse=True)
    watch.sort(key=lambda r: (r["severity"], r["safety"]), reverse=True)

    min_safety = MIN_BUY_SAFETY.get(mkt.zone)
    buy = []
    for sym in candidates if min_safety is not None else []:
        m = mkt.stocks[sym]
        if sym in flagged:
            continue
        news_score, _ = news_of(sym)
        d = days_to(sym)
        r = row(sym, m)
        checks = {
            "esticada": m["price"] / m["sma50"] - 1 <= MAX_STRETCH,
            "resultados": d is None or d > EARNINGS_BUY_DAYS,
            "noticias": news_score is None or news_score >= 0,
            "liquidez": (m.get("dollar_volume") or 0) >= MIN_DOLLAR_VOLUME,
            "risco": r["risk_pct"] <= MAX_RISK * 100,
            "seguranca": r["safety"] >= min_safety,
            "setor": r["sector_trend"] is None or r["sector_trend"] >= 0,
            "fundamentos": m.get("fund_q") is None or m["fund_q"] >= MIN_FUND_Q,
        }
        if not all(checks.values()):
            continue
        reasons = entry_signals(sym, m)
        if sym in ranked:
            reasons.append("🏆 top 3 do setor no ranking")
        if news_score and news_score >= 3:
            reasons.append("📰 notícias importantes e positivas")
        buy.append({**r, "reasons": reasons, "checks": [
            "✅ tendência de alta (Stage 2)", f"✅ {r['stretch_pct']:.0f}% acima da média de 50 dias (máx. {MAX_STRETCH * 100:.0f}%)",
            "✅ sem resultados nos próximos 10 dias" if d is None or d > EARNINGS_BUY_DAYS else "", "✅ sem notícias negativas",
            f"✅ stop a {r['risk_pct']:.1f}% (máx. {MAX_RISK * 100:.0f}%)", f"✅ score {r['safety']} (mín. {min_safety})",
            "✅ setor acima da média de 200 dias" if r["sector_trend"] is not None else "",
            f"✅ fundamentos: qualidade {m['fund_q']:.0f}/100 (mín. {MIN_FUND_Q})" if m.get("fund_q") is not None else ""]})
    for b in buy:
        b["checks"] = [c for c in b["checks"] if c]
    # Best first: safety, then several entry signals, then company quality.
    buy.sort(key=lambda r: (r["safety"] + 3 * (len(r["reasons"]) - 1) + 0.05 * (quality.get(r["symbol"]) or 50)), reverse=True)
    buy = buy[:MAX_BUY]

    etf_floor = min_safety if min_safety is not None else 101
    for sym, m in sorted(mkt.etfs.items(), key=lambda kv: kv[1].get("trend_score", 0), reverse=True)[:8]:
        if sym in flagged or not (m["price"] > m["sma50"] > m["sma200"] and m["sma200_rising"]) or m["price"] / m["sma50"] - 1 > MAX_STRETCH:
            continue
        r = row(sym, m, etf=True)
        if r["safety"] >= etf_floor and sum(b["etf"] for b in buy) < 3:
            buy.append({**r, "reasons": ["📦 um dos ETFs UCITS com melhor tendência"],
                        "checks": ["✅ tendência de alta", f"✅ {r['stretch_pct']:.0f}% acima da média de 50 dias", f"✅ score {r['safety']}"]})

    # The lists above pick who gets in; the page shows each one by safety score, highest first.
    by_score = lambda rows: sorted(rows, key=lambda r: r["safety"], reverse=True)  # noqa: E731
    return {
        "as_of": today.isoformat(), "zone": mkt.zone, "stage2_pct": round(mkt.stage2_share * 100, 1),
        "buy": by_score(buy), "watch": by_score(watch[:MAX_WATCH]), "sell": by_score(sell[:MAX_SELL]),
        "criteria": [{"key": k, "label": label, "max": mx, "rule": rule} for k, label, mx, rule in CRITERIA],
        "rules": {"buy": BUY_RULES, "sell": SELL_RULES, "watch": WATCH_RULES},
        "levels": [{"from": f, "name": n} for f, n in LEVELS],
    }
