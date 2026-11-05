"""One-off repair (November 2026): the daily runs of 3, 4 and 5 November ran before Europe opened, when Yahoo's daily
history of European listings still lacked the previous session. Those runs recorded each agent's point for 2, 3 and
4 November with European holdings, and the index (SXR8.DE, the points' benchmark), at the close of the session
before. This puts each point at its own day's close and redoes what follows from it: the day's points against the
index, the points total, the Maestro's excess per market zone, the peak and maximum drawdown, and the shadows'
birth figures. The daily run itself was fixed (agents.fill_last_day).

The plan (agents/lab/reparacao-2026-11.json, made from the private repo's git history) says the dates, each date's
market zone and which European symbols did have that day's bar. Run once; it marks the plan as done. Only counts are
printed (the Actions log is public).

    uv run python scripts/repair_european_days.py [--simular]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import pandas as pd

import agents

PLAN = agents.REPO_AGENTS / "lab" / "reparacao-2026-11.json"


def holdings_at(port: dict, day: str) -> dict[str, float]:
    """The quantity of each symbol held at the end of `day`: today's positions with the later trades undone."""
    held = {s: float(p["qty"]) for s, p in port.get("positions", {}).items()}
    for t in port.get("trades", []):
        if t["date"] <= day or t.get("side") not in ("compra", "venda"):
            continue
        held[t["symbol"]] = held.get(t["symbol"], 0.0) + (-t["qty"] if t["side"] == "compra" else t["qty"])
    return {s: q for s, q in held.items() if q > 1e-9}


class Closes:
    """Daily closes in euros, from Yahoo's (now complete) daily history."""

    def __init__(self, mkt: agents.Market):
        self.mkt = mkt

    def series(self, sym: str) -> pd.Series | None:
        df = self.mkt.hist.get(sym)
        if df is None or not len(df):
            return None
        s = df["Close"].copy()
        s.index = [d.date() for d in s.index]
        return s

    def on(self, sym: str, day: date) -> float | None:
        s = self.series(sym)
        return float(s[s.index <= day].iloc[-1]) if s is not None and (s.index <= day).any() else None

    def before(self, sym: str, day: date) -> float | None:
        s = self.series(sym)
        return float(s[s.index < day].iloc[-1]) if s is not None and (s.index < day).any() else None

    def eur(self, sym: str, price: float | None, day: date) -> float | None:
        return None if price is None else agents.to_eur(self.mkt, sym, price, day)


def repair_port(port: dict, plan: dict, closes: Closes, zones: dict) -> dict:
    """Fix one portfolio's points in the plan's dates. Returns {"equity": {date: delta}, "points": {date: delta}}."""
    hist = port.get("history") or []
    by_date = {h["date"]: i for i, h in enumerate(hist)}
    dates = [d for d in plan["dates"] if d in by_date]
    old = [dict(h) for h in hist]
    out = {"equity": {}, "points": {}}
    for d in dates:
        day = date.fromisoformat(d)
        present = set(plan["present"].get(d, []))
        delta = 0.0
        for sym, qty in holdings_at(port, d).items():
            if agents.exchange_of(sym) in ("", "=X") or sym in present:
                continue
            now, then = closes.on(sym, day), closes.before(sym, day)
            if now is None or then is None:
                continue
            delta += qty * (closes.eur(sym, now, day) - closes.eur(sym, then, day))
        if abs(delta) >= 0.005:
            hist[by_date[d]]["equity"] = round(hist[by_date[d]]["equity"] + delta, 2)
            out["equity"][d] = round(delta, 2)
    # the points of each repaired day, and of the day after a repaired one (its start moved)
    bench = agents.BENCHMARK
    redo = sorted({i for d in dates for i in (by_date[d], by_date[d] + 1) if 0 < i < len(hist)})
    peak, ratios = 1.0, []
    for h in hist:
        ratios.append(h["equity"] / h["deposits"] if h["deposits"] else 1.0)
    log = {e["date"]: e for e in port.get("points_log", [])}
    for i in redo:
        d, prev = hist[i]["date"], hist[i - 1]["date"]
        day, pday = date.fromisoformat(d), date.fromisoformat(prev)
        dep = hist[i]["deposits"] - hist[i - 1]["deposits"]
        new_day = (hist[i]["equity"] - dep) / hist[i - 1]["equity"] - 1 if hist[i - 1]["equity"] else 0.0
        old_day = (old[i]["equity"] - dep) / old[i - 1]["equity"] - 1 if old[i - 1]["equity"] else 0.0
        b_prev, b_now = closes.on(bench, pday), closes.on(bench, day)
        # the run of that day saw the index's previous session for the day itself when the day was a lagged one
        b_old_now = closes.before(bench, day) if d in plan["dates"] and bench not in plan["present"].get(d, []) else b_now
        new_bench = b_now / b_prev - 1 if b_prev and b_now else 0.0
        old_bench = b_old_now / b_prev - 1 if b_prev and b_old_now else 0.0
        new_excess, old_excess = new_day - new_bench, old_day - old_bench
        peak = max([1.0] + ratios[: i + 1])
        dd = max(0.0, 1 - ratios[i] / peak)
        pts = (max(-10, min(10, round(new_excess * 1000))) + (agents.PROFIT_BONUS if hist[i]["equity"] > hist[i]["deposits"] else 0)
               - round(max(0.0, dd - agents.DD_FREE) * agents.DD_PENALTY))
        if d in log:
            change = pts - log[d]["pts"]
            if change:
                log[d]["pts"] = pts
                port["points"] = port.get("points", 0) + change
                out["points"][d] = change
        zone = zones.get(d)
        zs = port.get("zone_stats", {}).get(zone) if zone else None
        if zs and abs(new_excess - old_excess) > 1e-12:
            zs[0] = round(zs[0] + new_excess - old_excess, 6)
    if out["equity"]:
        run_peak, max_dd = 1.0, 0.0
        for r in ratios:
            run_peak = max(run_peak, r)
            max_dd = max(max_dd, 1 - r / run_peak)
        port["peak"] = round(max(run_peak, ratios[-1] if ratios else 1.0), 6)
        port["max_dd"] = round(max_dd, 4)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--simular", action="store_true", help="calcular sem gravar")
    a = ap.parse_args()
    plan = json.loads(PLAN.read_text())
    if plan.get("feito"):
        print("Reparação já feita.")
        return 0
    config, state = agents.load_config(), agents.load_state()
    mkt = agents.load_market()  # complete daily history: every European session of the plan is there now
    closes = Closes(mkt)
    missing = [d for d in plan["dates"] if closes.on(agents.BENCHMARK, date.fromisoformat(d)) is None]
    if missing:
        print(f"::error::sem fecho do índice em {missing}: nada mudado")
        return 1
    zones = plan.get("zones", {})
    fixed_eq = fixed_pts = 0
    per_agent = {}
    for aid, port in state["agents"].items():
        r = repair_port(port, plan, closes, zones)
        per_agent[aid] = r
        fixed_eq += len(r["equity"])
        fixed_pts += len(r["points"])
    shadows = 0
    for aid, copies in (state.get("shadows") or {}).items():
        parent = state["agents"].get(aid)
        for sh in copies:
            repair_port(sh["port"], plan, closes, zones)
            born = sh.get("born")
            if parent and born in plan["dates"]:
                h = next((x for x in parent["history"] if x["date"] == born), None)
                if h and h["deposits"]:
                    sh["parent_ratio"] = round(h["equity"] / h["deposits"], 6)
                sh["parent_points"] = sh.get("parent_points", 0) + sum(
                    v for d, v in per_agent.get(aid, {}).get("points", {}).items() if d <= born)
            shadows += 1
    print(f"Pontos de valor corrigidos: {fixed_eq} · dias de pontos refeitos: {fixed_pts} · cópias-sombra vistas: {shadows}")
    if a.simular:
        return 0
    agents.save(config, state)
    plan["feito"] = True
    plan["resultado"] = {aid: r for aid, r in per_agent.items() if r["equity"] or r["points"]}
    PLAN.write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
