"""Buy and sell proposals from the agents' own decisions, with a realistic plan and a public track record.

Every weekday the agents decide what to buy and sell at the next open (agents.run_day). This module:

1. Weighs each agent by its record (WEIGHT_RULES, shown on the portal): the exam year of its pre-training,
   how it did in the current market zone, how it is doing live against the S&P 500, and its risk of ruin.
   The Indexador (always the index) and the meta-agents (who copy the others) do not vote.
2. Turns the day's buy orders into proposals: who proposes it, the weighted support (confidence), and a
   plan: maximum entry price, stop from the stock's volatility (2 ATR, at most 10%), target at 2.5R, the
   net result at the target after costs and 28% tax, typical holding time and the supporting agents'
   record (win rate, average win and loss). Sell orders become "the agents are selling" proposals.
3. Logs every buy proposal in agents/proposals.json and scores it on the following sessions: executed or
   not (the open must be at or below the entry price within 3 sessions), stop, target, or the result
   after 20 sessions. The scoreboard (hit rate, average R) is what keeps the proposals honest.

The portal turns the plan into a number of shares for each person's capital and risk.
"""

from __future__ import annotations

import json
import statistics
from datetime import date

import pandas as pd

from agents import (BENCHMARK, FEE, FX_FEE, LIVE_ONLY, META, REPO_AGENTS, TAX_RATE, Market, active_agents,
                    benchmark_return, is_eur, last_close, live_stats)
import opportunities

LOG_PATH = REPO_AGENTS / "proposals.json"
ATR_DAYS = 14
STOP_ATR = 2.0
MAX_STOP = 0.10
TARGET_R = 2.5
ENTRY_ATR = 0.25  # do not pay more than the close + a quarter of a day's range
FILL_WINDOW = 3  # sessions for the entry price to be reached
HORIZON = 20  # sessions a proposal is followed
LOG_KEPT = 400
NO_VOTE = META | {"indexador"}

WEIGHT_RULES = [
    "Todos começam com peso 1.",
    "Exame do pré-treino (último ano, fora do treino): pontos positivos somam até +1 (300 pontos = +1); negativos multiplicam por 0,6.",
    "Zona de mercado atual: se no pré-treino a estratégia bateu o S&P 500 nesta zona, soma até +1 (0,03% por dia = +1); se perdeu, multiplica por 0,6.",
    "Ao vivo (a partir de 20 sessões): à frente do S&P 500 soma até +1 (5 pontos percentuais = +1); atrás multiplica por 0,7.",
    "Risco de falência no Monte Carlo acima de 5%: multiplica por 0,5.",
    "O peso final fica entre 0,1 e 4. O Indexador e os agentes que copiam os outros (Conselho, Maestro) não votam.",
]


def load_pretrain() -> dict:
    path = REPO_AGENTS / "pretrain.json"
    return json.loads(path.read_text()).get("strategies", {}) if path.exists() else {}


def live_trade_stats(port: dict) -> dict:
    rets = []
    for t in port["trades"]:
        if t["side"] != "venda":
            continue
        cost = t["eur"] - t["pnl_eur"]
        rets.append(t.get("ret", t["pnl_eur"] / cost if cost else 0.0))
    if not rets:
        return {}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    held = sorted(t["held"] for t in port["trades"] if t["side"] == "venda" and t.get("held") is not None)
    return {"trades": len(rets), "win_rate": round(len(wins) / len(rets), 3),
            "avg_win": round(statistics.fmean(wins), 4) if wins else 0.0,
            "avg_loss": round(statistics.fmean(losses), 4) if losses else 0.0,
            "median_days": held[len(held) // 2] if held else None}


def agent_weight(cfg: dict, port: dict, mkt: Market, pre: dict) -> tuple[float, list[str]]:
    """(weight, the reasons) following WEIGHT_RULES."""
    w, why = 1.0, []
    r = pre.get(cfg["strategy"]) or {}
    exam = ((r.get("valid") or {}).get("best") or {}).get("points") if r.get("adopted") else ((r.get("valid") or {}).get("default") or {}).get("points")
    if exam is not None:
        if exam > 0:
            w += min(1.0, exam / 300)
            why.append(f"exame +{exam} pts")
        else:
            w *= 0.6
            why.append(f"exame {exam} pts")
    prior = (mkt.priors.get(cfg["strategy"]) or {}).get(mkt.zone)
    if prior:
        if prior > 0:
            w += min(1.0, prior / 0.0003)
            why.append(f"bate o S&P 500 em zona {mkt.zone} ({prior * 100:+.3f}%/dia)")
        else:
            w *= 0.6
            why.append(f"perde para o S&P 500 em zona {mkt.zone} ({prior * 100:+.3f}%/dia)")
    st = live_stats(port)
    if st["sessions"] >= 20 and port["history"]:
        bench = benchmark_return(mkt, date.fromisoformat(port["history"][0]["date"]), mkt.as_of)
        excess = st["return"] - bench
        if excess > 0:
            w += min(1.0, excess / 0.05)
        else:
            w *= 0.7
        why.append(f"ao vivo {excess * 100:+.1f} p.p. vs S&P 500")
    ruin = (r.get("monte_carlo") or {}).get("p_ruin")
    if ruin is not None and ruin > 0.05:
        w *= 0.5
        why.append(f"falência no Monte Carlo {ruin:.0%}")
    if cfg["strategy"] in LIVE_ONLY and not why:
        why.append("só aprende ao vivo (sem pré-treino)")
    return round(min(4.0, max(0.1, w)), 2), why


def atr(df: pd.DataFrame, n: int = ATR_DAYS) -> float | None:
    if df is None or len(df) < n + 1:
        return None
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    return float(tr.iloc[-n:].mean())


def fx_eur(mkt: Market, symbol: str) -> float:
    """Euros per unit of the symbol's currency today."""
    if is_eur(symbol):
        return 1.0
    rate = mkt.fx.iloc[-1] if len(mkt.fx) else None
    return 1.0 / float(rate) if rate else 1.0


def plan(mkt: Market, symbol: str, orders: list[dict]) -> dict | None:
    """Entry, stop and target in the stock's currency, plus net result at the target."""
    price = last_close(mkt, symbol)
    a = atr(mkt.hist.get(symbol))
    if not price or not a:
        return None
    entry = price + ENTRY_ATR * a
    stop = max(price - STOP_ATR * a, price * (1 - MAX_STOP))
    agent_stops = sorted(o["stop"] for o in orders if o.get("stop") and price * (1 - 0.25) < o["stop"] < price)
    if agent_stops:  # the agents' own stops, when they set them, if tighter than ours
        stop = max(stop, agent_stops[len(agent_stops) // 2])
    risk = entry - stop
    target = entry + TARGET_R * risk
    agent_tps = sorted(o["tp"] for o in orders if o.get("tp") and o["tp"] > entry)
    if agent_tps:
        target = agent_tps[len(agent_tps) // 2]
    costs = 2 * (FEE + (0.0 if is_eur(symbol) else FX_FEE))
    gross = target / entry - 1
    net = (gross - costs) * (1 - TAX_RATE) if gross > costs else gross - costs
    loss = -(risk / entry) - costs
    return {
        "price": round(price, 2), "entry_max": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
        "rr": round((target - entry) / risk, 2) if risk > 0 else None, "atr": round(a, 2),
        "risk_pct": round(risk / entry * 100, 2), "gross_pct": round(gross * 100, 2),
        "net_target_pct": round(net * 100, 2), "net_stop_pct": round(loss * 100, 2),
        "currency": "EUR" if is_eur(symbol) else "USD", "fx_eur": round(fx_eur(mkt, symbol), 5),
    }


def build(config: dict, state: dict, mkt: Market) -> dict:
    """Today's proposals from the orders the agents just placed for the next open."""
    pre = load_pretrain()
    today = mkt.as_of.isoformat()
    voters, total = [], 0.0
    for cfg in active_agents(config):
        port = state["agents"].get(cfg["id"])
        if not port or cfg["strategy"] in NO_VOTE:
            continue
        w, why = agent_weight(cfg, port, mkt, pre)
        stats = (((pre.get(cfg["strategy"]) or {}).get("year") or {}).get("trade_stats")) or live_trade_stats(port)
        voters.append({"id": cfg["id"], "name": cfg["name"], "emoji": cfg["emoji"], "weight": w, "why": why,
                       "stats": stats, "port": port, "cfg": cfg})
        total += w
    buys: dict[str, list] = {}
    sells: dict[str, list] = {}
    for v in voters:
        for o in v["port"]["pending"]:
            if o.get("created") != today:
                continue
            (buys if o["side"] == "buy" else sells).setdefault(o["symbol"], []).append((v, o))

    quality = {r["symbol"]: r.get("quality_score") for r in mkt.rankings}

    def safety_of(sym: str) -> tuple[int | None, str | None, list]:
        m = mkt.stocks.get(sym) or mkt.etfs.get(sym)
        if not m:
            return None, None, []
        d = mkt.earnings.get(sym)
        days = (d - mkt.as_of).days if isinstance(d, date) and d >= mkt.as_of else None
        score, parts = opportunities.safety(m, quality.get(sym), days, (mkt.news.get(sym) or {}).get("score"), mkt.zone,
                                            etf=sym in mkt.etfs)
        return score, opportunities.level(score), parts

    def record(votes: list) -> dict:
        """The supporting agents' trade record, weighted by their weight."""
        rows = [(v["weight"], v["stats"]) for v, _ in votes if v["stats"].get("trades")]
        if not rows:
            return {}
        wsum = sum(w for w, _ in rows)
        avg = lambda k: sum(w * (s.get(k) or 0) for w, s in rows) / wsum  # noqa: E731
        days = [s["median_days"] for _, s in rows if s.get("median_days")]
        return {"win_rate": round(avg("win_rate"), 3), "avg_win": round(avg("avg_win"), 4), "avg_loss": round(avg("avg_loss"), 4),
                "trades": sum(s["trades"] for _, s in rows), "median_days": sorted(days)[len(days) // 2] if days else None}

    out_buys = []
    for sym, votes in buys.items():
        p = plan(mkt, sym, [o for _, o in votes])
        if not p:
            continue
        support = sum(v["weight"] for v, _ in votes)
        m = mkt.stocks.get(sym) or mkt.etfs.get(sym) or {}
        score, level, parts = safety_of(sym)
        d = mkt.earnings.get(sym)
        sell_sig, watch_sig = opportunities.flags(m, (d - mkt.as_of).days if isinstance(d, date) and d >= mkt.as_of else None,
                                                  (mkt.news.get(sym) or {}).get("score")) if m else ([], [])
        out_buys.append({
            "symbol": sym, "name": m.get("name", ""), "sector": m.get("sector", ""), "etf": sym in mkt.etfs,
            "support": round(support, 2), "confidence": round(support / total * 100, 1) if total else 0.0,
            "agents": [{"name": v["name"], "emoji": v["emoji"], "weight": v["weight"], "reason": o.get("reason", "")} for v, o in votes],
            "plan": p, "record": record(votes), "safety": score, "level": level, "parts": parts,
            "earnings": mkt.earnings[sym].isoformat() if isinstance(mkt.earnings.get(sym), date) else None,
            "news": ((mkt.news.get(sym) or {}).get("items") or [{}])[0].get("resumo", ""),
            # the same signals as the Oportunidades tab: a proposal against them is shown with the warning, at the end
            "alerts": sell_sig + watch_sig, "conflict": "venda" if sell_sig else "atencao" if watch_sig else None,
        })
    out_buys.sort(key=lambda r: (r["conflict"] is None, r["support"], r["safety"] or 0), reverse=True)

    out_sells = []
    for sym, votes in sells.items():
        m = mkt.stocks.get(sym) or mkt.etfs.get(sym) or {}
        score, level, parts = safety_of(sym)
        out_sells.append({
            "symbol": sym, "name": m.get("name", ""), "support": round(sum(v["weight"] for v, _ in votes), 2),
            "agents": [{"name": v["name"], "emoji": v["emoji"], "weight": v["weight"], "reason": o.get("reason", "")} for v, o in votes],
            "price": round(last_close(mkt, sym) or 0, 2), "safety": score, "level": level, "parts": parts,
        })
    out_sells.sort(key=lambda r: r["support"], reverse=True)

    held: dict[str, float] = {}
    for v in voters:
        for sym in v["port"]["positions"]:
            held[sym] = held.get(sym, 0.0) + v["weight"]
    return {
        "as_of": today, "zone": mkt.zone, "total_weight": round(total, 2), "buys": out_buys, "sells": out_sells,
        "weights": sorted([{"name": v["name"], "emoji": v["emoji"], "weight": v["weight"], "why": v["why"], "stats": v["stats"]}
                           for v in voters], key=lambda x: x["weight"], reverse=True),
        "held": sorted(({"symbol": s, "support": round(w, 2)} for s, w in held.items()), key=lambda x: x["support"], reverse=True)[:15],
        "rules": WEIGHT_RULES,
        "method": {"entry_atr": ENTRY_ATR, "stop_atr": STOP_ATR, "max_stop": MAX_STOP, "target_r": TARGET_R,
                   "fill_window": FILL_WINDOW, "horizon": HORIZON, "tax": TAX_RATE},
    }


# --------------------------------------------------------------------------
# Track record
# --------------------------------------------------------------------------


def load_log() -> list[dict]:
    return json.loads(LOG_PATH.read_text()) if LOG_PATH.exists() else []


def bucket(confidence: float) -> str:
    return "alta" if confidence >= 40 else "média" if confidence >= 20 else "baixa"


def follow(entry: dict, df: pd.DataFrame | None) -> dict:
    """Score one logged proposal on the sessions after it was made."""
    if df is None or entry["status"] in ("alvo", "stop", "expirou", "não executada"):
        return entry
    after = df[df.index.date > date.fromisoformat(entry["date"])]
    if after.empty:
        return entry
    e = dict(entry)
    i0 = None
    for i in range(min(FILL_WINDOW, len(after))):
        bar = after.iloc[i]
        if bar["Low"] <= e["entry_max"]:
            i0, e["fill"] = i, round(min(float(bar["Open"]), e["entry_max"]), 4)
            e["fill_date"] = after.index[i].date().isoformat()
            break
    if i0 is None:
        if len(after) >= FILL_WINDOW:
            e["status"] = "não executada"
        return e
    risk = e["fill"] - e["stop"]
    e["status"] = "aberta"
    for k, (idx, bar) in enumerate(after.iloc[i0:].iterrows()):
        if bar["Low"] <= e["stop"]:
            e.update(status="stop", exit=e["stop"], exit_date=idx.date().isoformat())
            break
        if bar["High"] >= e["target"]:
            e.update(status="alvo", exit=e["target"], exit_date=idx.date().isoformat())
            break
        e["last"] = round(float(bar["Close"]), 4)
        if k == 4:
            e["ret_5"] = round(e["last"] / e["fill"] - 1, 4)
        if k + 1 >= HORIZON:
            e.update(status="expirou", exit=e["last"], exit_date=idx.date().isoformat())
            break
    if e.get("exit") is not None:
        e["ret"] = round(e["exit"] / e["fill"] - 1, 4)
        e["r"] = round((e["exit"] - e["fill"]) / risk, 2) if risk > 0 else None
    elif e.get("last"):
        e["ret_open"] = round(e["last"] / e["fill"] - 1, 4)
    return e


def update_log(proposals: dict, mkt: Market) -> list[dict]:
    """Follow the logged proposals one more day and add today's; keeps the last LOG_KEPT."""
    log = [follow(e, mkt.hist.get(e["symbol"])) for e in load_log()]
    seen = {(e["date"], e["symbol"]) for e in log}
    for b in proposals["buys"]:
        if (proposals["as_of"], b["symbol"]) in seen:
            continue
        log.append({"date": proposals["as_of"], "symbol": b["symbol"], "confidence": b["confidence"],
                    "bucket": bucket(b["confidence"]), "agents": [a["name"] for a in b["agents"]],
                    "entry_max": b["plan"]["entry_max"], "stop": b["plan"]["stop"], "target": b["plan"]["target"],
                    "status": "à espera"})
    log = log[-LOG_KEPT:]
    LOG_PATH.write_text(json.dumps(log, indent=1, ensure_ascii=False) + "\n")
    return log


def scoreboard(log: list[dict]) -> dict:
    def stats(rows: list[dict]) -> dict:
        done = [e for e in rows if e["status"] in ("alvo", "stop", "expirou")]
        executed = [e for e in rows if e.get("fill") is not None]
        wins = [e for e in done if (e.get("ret") or 0) > 0]
        rs = [e["r"] for e in done if e.get("r") is not None]
        return {"proposals": len(rows), "executed": len(executed), "closed": len(done),
                "open": sum(e["status"] == "aberta" for e in rows),
                "targets": sum(e["status"] == "alvo" for e in done), "stops": sum(e["status"] == "stop" for e in done),
                "win_rate": round(len(wins) / len(done), 3) if done else None,
                "avg_r": round(statistics.fmean(rs), 2) if rs else None,
                "avg_ret": round(statistics.fmean([e["ret"] for e in done]) * 100, 2) if done else None}
    return {
        "all": stats(log),
        "by_bucket": {b: stats([e for e in log if e.get("bucket") == b]) for b in ("alta", "média", "baixa")},
        "recent": list(reversed(log[-40:])),
    }
