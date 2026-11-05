"""Interactive Brokers: one agent's portfolio followed by an IBKR account (paper first), and the same orders simulated
for the agents under test, "as if traded there".

How it follows: not trade by trade but by weights. Each cycle the account's positions are brought to the agent's
weights (its positions plus the orders it left for the next open), scaled to the account's net liquidation value; a
cycle that failed is made up by the next one. The same plan drives three accounts:

- virtual (`virtual_round`, run by agents.py in the rounds and the daily run): an IBKR account kept in the private
  repo (agents/lab/ibkr/virtual/<agent>.json) for the agents listed in config "ibkr" (default: the frozen candidates).
  Fills at the round's price with IBKR's costs (agents.BROKERS["ibkr"]) and IBKR's order rules (whole shares unless
  "fracoes", minimum order, tolerance band), so the gap between the agent and what an account can really do shows.
- paper / real (`main`, run on this PC): the IBKR Client Portal Gateway (IBKR's program for retail API access; it
  runs locally and you sign in through its web page, with 2FA for a real account). Credentials never touch this code
  or the repo: the script only talks to the gateway already signed in on https://localhost:5000.

Safeguards: paper only unless config ibkr.real is true AND IBKR_REAL=1 and the account is not a paper one (DU...);
the file agents/lab/ibkr/PARAR stops every order; at most max_ordens orders and max_ordem_eur per order per cycle,
and max_giro of the account traded per day; orders only while the stock's exchange is open, as marketable limit
orders (limite_pct away from the reference price); IBKR's confirmation questions are answered yes only on paper (on a
real account, with --confirmar). Every plan and answer goes to agents/lab/ibkr/registo-<YYYY-MM>.jsonl (private).

    uv run python scripts/ibkr.py estado                       # is the gateway signed in? which account?
    uv run python scripts/ibkr.py plano --agente <id>          # the orders it would send now (nothing sent)
    uv run python scripts/ibkr.py seguir --agente <id> [--ciclo 600] [--push]   # send them, every 10 min
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import agents

LAB = agents.REPO_AGENTS / "lab" / "ibkr"
VIRTUAL = LAB / "virtual"
CONIDS = LAB / "conids.json"  # symbol -> IBKR contract id, found once (and fixable by hand)
STOP_FILE = LAB / "PARAR"
GATEWAY = os.environ.get("IBKR_GATEWAY", "https://localhost:5000/v1/api")

DEFAULTS = {"capital_eur": None, "fracoes": False, "min_ordem_eur": 100.0, "banda": 0.01, "max_ordem_eur": 2500.0,
            "max_ordens": 10, "max_giro": 0.6, "limite_pct": 0.005, "real": False}

# Yahoo suffix -> IBKR listing exchange and currency (US listings go through SMART routing in USD)
LISTINGS = {".DE": ("IBIS", "EUR"), ".F": ("FWB", "EUR"), ".MC": ("BM", "EUR"), ".PA": ("SBF", "EUR"),
            ".AS": ("AEB", "EUR"), ".MI": ("BVME", "EUR"), ".BR": ("ENEXT.BE", "EUR"), ".LS": ("ENEXT.LS", "EUR"),
            ".HE": ("HEX", "EUR"), ".L": ("LSE", "GBP"), ".SW": ("EBS", "CHF"), ".CO": ("CPH", "DKK"),
            ".ST": ("SFB", "SEK"), ".OL": ("OSE", "NOK")}


def rules(config: dict | None = None) -> dict:
    """config "ibkr" over the defaults; the agents followed default to the frozen candidates."""
    config = config if config is not None else agents.load_config()
    r = {**DEFAULTS, **(config.get("ibkr") or {})}
    if not r.get("agentes"):
        r["agentes"] = [c["id"] for c in config.get("agents", []) if c.get("congelado") and c.get("active", True)]
    r["capital_eur"] = float(r.get("capital_eur") or agents.real_capital())
    return r


def listing(symbol: str) -> tuple[str, str, str]:
    """(IBKR ticker, exchange, currency) of a Yahoo symbol: SAP.DE -> (SAP, IBIS, EUR); BRK-B -> (BRK B, SMART, USD)."""
    for suffix, (exchange, currency) in LISTINGS.items():
        if symbol.endswith(suffix):
            return symbol[: -len(suffix)].replace("-", " "), exchange, currency
    return symbol.replace("-", " "), "SMART", "USD"


def commission_eur(symbol: str, value_eur: float, qty: float, eur_per_unit: float) -> float:
    """IBKR's commission of one order (agents.BROKERS["ibkr"]), in euros; eur_per_unit: euros per unit of the stock's
    currency."""
    b = agents.BROKERS["ibkr"]
    if agents.is_us(symbol):
        u = b["us"]
        return min(max(u["min"], u["por_acao"] * qty) * eur_per_unit, u["max_pct"] * value_eur)
    e = b["eu"]
    return min(max(e["pct"] * value_eur, e["min_eur"]), e["max_eur"])


def slippage(symbol: str) -> float:
    """Spread per side, plus currency conversion outside the euro (IBKR's terms)."""
    b = agents.BROKERS["ibkr"]
    return b["spread"] + agents.LIQUIDITY_EXTRA.get(symbol, 0.0) + (0.0 if agents.is_eur(symbol) else b["fx"])


# ---------------------------------------------------------------------------
# What to hold: the agent's weights
# ---------------------------------------------------------------------------


def target_weights(port: dict, values_eur: dict[str, float]) -> dict[str, float]:
    """The agent's weight per symbol, of its whole equity, after the orders it left for the next open.

    values_eur: each position's value now, in euros (by agents.position_value or a fresh price)."""
    held = {s: v for s, v in values_eur.items() if s in port.get("positions", {})}
    cash = float(port.get("cash", 0.0))
    for o in port.get("pending", []):
        s = o["symbol"]
        if o["side"] == "sell" and s in held:
            sold = held[s] * float(o.get("fraction", 1.0))
            held[s] -= sold
            cash += sold
        elif o["side"] == "buy":
            eur = min(float(o.get("eur") or 0.0), max(cash, 0.0))
            held[s] = held.get(s, 0.0) + eur
            cash -= eur
    total = cash + sum(held.values())
    return {s: v / total for s, v in held.items() if v > 0} if total > 0 else {}


@dataclass
class Account:
    """An account as the plan sees it: its value, cash and positions, all in euros."""
    nlv_eur: float
    cash_eur: float
    positions: dict[str, float] = field(default_factory=dict)  # symbol -> quantity
    prices: dict[str, float] = field(default_factory=dict)  # symbol -> price in its currency
    eur_per_unit: dict[str, float] = field(default_factory=dict)  # symbol -> euros per unit of its currency

    def value(self, s: str) -> float:
        return self.positions.get(s, 0.0) * self.prices.get(s, 0.0) * self.eur_per_unit.get(s, 1.0)


def plan(weights: dict[str, float], acct: Account, r: dict, scale: float | None = None) -> list[dict]:
    """The orders that bring the account to the weights: sells first, then buys within the cash.

    A difference under max(min_ordem_eur, banda x account) is left alone, except a stock the agent no longer holds,
    which is sold whole. Quantities are whole shares unless "fracoes"."""
    base = acct.nlv_eur * (scale if scale is not None else 1.0)
    floor_eur = max(r["min_ordem_eur"], r["banda"] * base)
    sells, buys = [], []
    for s in sorted(set(weights) | {s for s, q in acct.positions.items() if q > 0}):
        price, fx = acct.prices.get(s), acct.eur_per_unit.get(s, 1.0)
        if not price or price <= 0:
            continue
        unit = price * fx
        diff = weights.get(s, 0.0) * base - acct.value(s)
        held = acct.positions.get(s, 0.0)
        if weights.get(s, 0.0) <= 0 and held > 0:
            qty = held
        elif abs(diff) < floor_eur:
            continue
        else:
            qty = abs(diff) / unit
            qty = round(qty, 4) if r["fracoes"] else float(math.floor(qty + 1e-9))
            if diff < 0:
                qty = min(qty, held)
        if qty <= 0:
            continue
        eur = qty * unit
        if eur > r["max_ordem_eur"]:
            qty = round(r["max_ordem_eur"] / unit, 4) if r["fracoes"] else float(math.floor(r["max_ordem_eur"] / unit))
            eur = qty * unit
            if qty <= 0:
                continue
        row = {"symbol": s, "side": "SELL" if diff < 0 or weights.get(s, 0.0) <= 0 else "BUY", "qty": qty,
               "ref": round(price, 4), "eur": round(eur, 2), "peso_alvo": round(weights.get(s, 0.0), 4),
               "peso_atual": round(acct.value(s) / acct.nlv_eur, 4) if acct.nlv_eur else 0.0}
        (sells if row["side"] == "SELL" else buys).append(row)
    cash = acct.cash_eur + sum(o["eur"] * (1 - slippage(o["symbol"])) for o in sells)
    kept = []
    for o in sorted(buys, key=lambda o: -o["eur"]):
        cost = o["eur"] * (1 + slippage(o["symbol"])) + commission_eur(o["symbol"], o["eur"], o["qty"],
                                                                       acct.eur_per_unit.get(o["symbol"], 1.0))
        if cost > cash:  # what fits
            unit = o["ref"] * acct.eur_per_unit.get(o["symbol"], 1.0)
            room = cash * 0.995 - commission_eur(o["symbol"], cash, cash / unit, acct.eur_per_unit.get(o["symbol"], 1.0))
            qty = room / unit / (1 + slippage(o["symbol"]))
            qty = round(qty, 4) if r["fracoes"] else float(math.floor(qty))
            if qty <= 0 or qty * unit < r["min_ordem_eur"]:
                continue
            o = {**o, "qty": qty, "eur": round(qty * unit, 2)}
            cost = o["eur"] * (1 + slippage(o["symbol"])) + commission_eur(o["symbol"], o["eur"], qty,
                                                                           acct.eur_per_unit.get(o["symbol"], 1.0))
        cash -= cost
        kept.append(o)
    return (sells + kept)[: r["max_ordens"]]


# ---------------------------------------------------------------------------
# The virtual IBKR account of each agent under test
# ---------------------------------------------------------------------------


def _load_virtual(aid: str, capital: float, today: str) -> dict:
    path = VIRTUAL / f"{aid}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"agente": aid, "desde": today, "deposito_eur": capital, "cash_eur": capital, "positions": {},
                "custos_eur": 0.0, "ordens": 0, "history": []}


def fill_virtual(v: dict, acct: Account, orders: list[dict]) -> list[dict]:
    """Execute the orders on the virtual account at the reference price, with IBKR's spread, FX and commission."""
    done = []
    for o in orders:
        s, fx = o["symbol"], acct.eur_per_unit.get(o["symbol"], 1.0)
        sign = 1 if o["side"] == "BUY" else -1
        fill = o["ref"] * (1 + sign * slippage(s))
        gross = o["qty"] * fill * fx
        fee = commission_eur(s, gross, o["qty"], fx)
        if sign > 0:
            if gross + fee > v["cash_eur"] + 1e-6:
                continue
            v["cash_eur"] -= gross + fee
            v["positions"][s] = v["positions"].get(s, 0.0) + o["qty"]
        else:
            v["cash_eur"] += gross - fee
            left = v["positions"].get(s, 0.0) - o["qty"]
            if left <= 1e-9:
                v["positions"].pop(s, None)
            else:
                v["positions"][s] = left
        v["custos_eur"] = round(v.get("custos_eur", 0.0) + fee + abs(fill - o["ref"]) * o["qty"] * fx, 4)
        v["ordens"] = v.get("ordens", 0) + 1
        done.append({**o, "fill": round(fill, 4), "comissao_eur": round(fee, 2)})
    return done


def _market_account(v: dict, mkt, symbols: set[str]) -> Account:
    prices, fx = {}, {}
    for s in symbols:
        p = agents.last_close(mkt, s)
        if p:
            prices[s] = p
            fx[s] = agents.to_eur(mkt, s, 1.0, mkt.as_of)
    acct = Account(nlv_eur=0.0, cash_eur=v["cash_eur"], positions=dict(v["positions"]), prices=prices, eur_per_unit=fx)
    acct.nlv_eur = acct.cash_eur + sum(acct.value(s) for s in acct.positions)
    return acct


def virtual_round(config: dict, state: dict, mkt, now: datetime | None = None) -> dict[str, dict]:
    """Bring each followed agent's virtual IBKR account to the agent's weights; keep it in the private repo.
    Returns {agent: {"nlv_eur", "ordens"}} (the figures stay out of the Actions log)."""
    r = rules(config)
    today = mkt.as_of.isoformat()
    out = {}
    for aid in r["agentes"]:
        port = state["agents"].get(aid)
        if not port:
            continue
        v = _load_virtual(aid, r["capital_eur"], today)
        symbols = set(port.get("positions", {})) | {o["symbol"] for o in port.get("pending", [])} | set(v["positions"])
        acct = _market_account(v, mkt, symbols)
        values = {s: agents.position_value(mkt, s, p) for s, p in port.get("positions", {}).items()}
        weights = target_weights(port, values)
        done = fill_virtual(v, acct, plan(weights, acct, r))
        acct = _market_account(v, mkt, set(v["positions"]))
        hist = v.setdefault("history", [])
        point = {"date": today, "nlv_eur": round(acct.nlv_eur, 2)}
        if hist and hist[-1]["date"] == today:
            hist[-1] = point
        else:
            hist.append(point)
        if done:
            v.setdefault("operacoes", []).extend({"at": (now or datetime.now(UTC)).isoformat(timespec="minutes"), **d}
                                                 for d in done)
            v["operacoes"] = v["operacoes"][-500:]
        v["atualizado"] = today
        try:
            VIRTUAL.mkdir(parents=True, exist_ok=True)
            (VIRTUAL / f"{aid}.json").write_text(json.dumps(v, ensure_ascii=False, indent=1) + "\n")
        except OSError:
            pass
        out[aid] = {"nlv_eur": round(acct.nlv_eur, 2), "ordens": len(done)}
    return out


def virtual_summary(aid: str) -> dict | None:
    """The virtual account's return since it started, for the decision page."""
    try:
        v = json.loads((VIRTUAL / f"{aid}.json").read_text())
    except (OSError, ValueError):
        return None
    if not v.get("history"):
        return None
    last = v["history"][-1]["nlv_eur"]
    return {"desde": v["desde"], "retorno": round(last / v["deposito_eur"] - 1, 4), "custos_eur": round(v.get("custos_eur", 0.0), 2),
            "ordens": v.get("ordens", 0), "posicoes": len(v.get("positions", {}))}


# ---------------------------------------------------------------------------
# The Client Portal Gateway (paper or real account)
# ---------------------------------------------------------------------------


class Gateway:
    """The few Client Portal Web API calls this needs. The gateway runs on this PC with a self-signed certificate,
    so TLS is only relaxed for localhost."""

    def __init__(self, base: str = GATEWAY, session=None):
        import requests

        self.base = base.rstrip("/")
        self.http = session or requests.Session()
        self.verify = not any(h in base for h in ("://localhost", "://127.0.0.1"))
        self._conids = None
        self.account = None

    def _call(self, method: str, path: str, **kw):
        resp = self.http.request(method, self.base + path, verify=self.verify, timeout=30, **kw)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def status(self) -> dict:
        return self._call("POST", "/iserver/auth/status") or {}

    def tickle(self) -> None:
        self._call("POST", "/tickle")

    def accounts(self) -> list[str]:
        rows = self._call("GET", "/portfolio/accounts") or []
        return [a["id"] for a in rows if a.get("id")]

    def use(self, account: str) -> None:
        self.account = account

    def summary(self) -> tuple[float, float]:
        """(net liquidation, cash), in the account's base currency (EUR expected)."""
        led = self._call("GET", f"/portfolio/{self.account}/ledger") or {}
        base = led.get("BASE") or {}
        return float(base.get("netliquidationvalue") or 0.0), float(base.get("cashbalance") or 0.0)

    def positions(self) -> list[dict]:
        out, page = [], 0
        while True:
            rows = self._call("GET", f"/portfolio/{self.account}/positions/{page}") or []
            out += rows
            if len(rows) < 100:
                return out
            page += 1

    def conid(self, symbol: str) -> int | None:
        if self._conids is None:
            try:
                self._conids = json.loads(CONIDS.read_text())
            except (OSError, ValueError):
                self._conids = {}
        if symbol in self._conids:
            return self._conids[symbol]
        ticker, exchange, _ = listing(symbol)
        found = (self._call("GET", "/trsrv/stocks", params={"symbols": ticker}) or {}).get(ticker) or []
        best = None
        for entry in found:
            for c in entry.get("contracts", []):
                if exchange == "SMART" and c.get("isUS") or c.get("exchange") == exchange:
                    best = c["conid"]
                    break
            if best:
                break
        if best:
            self._conids[symbol] = best
            CONIDS.parent.mkdir(parents=True, exist_ok=True)
            CONIDS.write_text(json.dumps(self._conids, indent=1, sort_keys=True) + "\n")
        return best

    def place(self, order: dict, confirm: bool) -> dict:
        """One marketable limit order (DAY); IBKR's questions answered yes only when confirm."""
        conid = self.conid(order["symbol"])
        if not conid:
            return {"erro": "contrato não encontrado"}
        _, exchange, _ = listing(order["symbol"])
        body = {"orders": [{"conid": conid, "orderType": "LMT", "side": order["side"], "quantity": order["qty"],
                            "price": order["limit"], "tif": "DAY", "listingExchange": exchange if exchange != "SMART" else None,
                            "cOID": order["coid"]}]}
        body["orders"][0] = {k: v for k, v in body["orders"][0].items() if v is not None}
        answer = self._call("POST", f"/iserver/account/{self.account}/orders", json=body)
        for _ in range(5):  # "are you sure?" questions (price far, size, ...)
            if not (isinstance(answer, list) and answer and answer[0].get("id") and answer[0].get("message")):
                break
            if not confirm:
                return {"pergunta": answer[0]["message"]}
            answer = self._call("POST", f"/iserver/reply/{answer[0]['id']}", json={"confirmed": True})
        first = answer[0] if isinstance(answer, list) and answer else answer or {}
        return {"order_id": first.get("order_id"), "estado": first.get("order_status"), "erro": first.get("error")}


def is_paper(account: str) -> bool:
    return account.upper().startswith("DU")


def market_open(symbol: str, now: datetime) -> bool:
    """Regular session only (orders go DAY, no pre/after-market)."""
    from zoneinfo import ZoneInfo

    zone, opens, closes = (("America/New_York", (9, 30), (15, 55)) if agents.is_us(symbol)
                           else ("Europe/Berlin", (9, 0), (17, 25)))
    t = now.astimezone(ZoneInfo(zone))
    return t.weekday() < 5 and opens <= (t.hour, t.minute) <= closes


def fresh_prices(symbols: set[str]) -> tuple[dict[str, float], dict[str, float]]:
    """(price in its currency, euros per unit of its currency) of each symbol, from Yahoo (a few symbols)."""
    import yfinance as yf

    prices, fx = {}, {}
    rates = {"EUR": 1.0}
    for s in sorted(symbols):
        try:
            hist = yf.Ticker(s).history(period="5d", interval="1d")
            prices[s] = float(hist["Close"].dropna().iloc[-1])
        except Exception:  # noqa: BLE001 - a missing price leaves the symbol out of this cycle
            continue
        cur = agents.currency_of(s)
        if cur not in rates:
            try:
                rate = yf.Ticker(f"EUR{cur.upper()}=X").history(period="5d")["Close"].dropna().iloc[-1]
                rates[cur] = 1.0 / float(rate)
            except Exception:  # noqa: BLE001
                rates[cur] = None
        if rates.get(cur):
            fx[s] = rates[cur] * (0.01 if cur in ("GBp", "GBX") else 1.0)
        else:
            prices.pop(s, None)
    return prices, fx


def log(entry: dict) -> None:
    LAB.mkdir(parents=True, exist_ok=True)
    with open(LAB / f"registo-{datetime.now(UTC):%Y-%m}.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def traded_today(account: str) -> float:
    """Euros already sent today from this account (for max_giro)."""
    total, today = 0.0, datetime.now(UTC).date().isoformat()
    try:
        lines = (LAB / f"registo-{datetime.now(UTC):%Y-%m}.jsonl").read_text().splitlines()
    except OSError:
        return 0.0
    try:
        for line in lines:
            e = json.loads(line)
            if e.get("conta") == account[-3:] and e.get("at", "").startswith(today) and e.get("enviado"):
                total += sum(o["eur"] for o in e.get("ordens", []) if o.get("resultado", {}).get("order_id"))
    except ValueError:
        pass
    return total


def cycle(gw: Gateway, aid: str, r: dict, send: bool, confirm: bool, now: datetime | None = None,
          prices=None) -> dict:
    """One pass: the account's state, the agent's weights, the orders; sent only when send and every check passes."""
    now = now or datetime.now(UTC)
    st = gw.status()
    if not st.get("authenticated"):
        return {"erro": "o gateway não tem sessão: abre https://localhost:5000 e entra na IBKR"}
    account = gw.account or (gw.accounts() or [None])[0]
    if not account:
        return {"erro": "sem conta na sessão"}
    gw.use(account)
    real = not is_paper(account)
    if real and not (r["real"] and os.environ.get("IBKR_REAL") == "1"):
        return {"erro": "conta real: só com ibkr.real=true no config e IBKR_REAL=1"}
    state = agents.load_state()
    port = state["agents"].get(aid)
    if not port:
        return {"erro": f"agente {aid} não existe"}
    for s in set(port.get("positions", {})) | {o["symbol"] for o in port.get("pending", [])}:
        gw.conid(s)  # known before reading the account, so its positions are recognised
    by_conid = {c: s for s, c in (gw._conids or {}).items()}
    held, unknown = {}, []
    for p in gw.positions():
        if not p.get("position"):
            continue
        if p.get("conid") in by_conid:
            held[by_conid[p["conid"]]] = float(p["position"])
        else:  # bought by hand: never touched
            unknown.append(p.get("contractDesc") or p.get("conid"))
    symbols = set(port.get("positions", {})) | {o["symbol"] for o in port.get("pending", [])} | set(held)
    price, fx = prices if prices is not None else fresh_prices(symbols)
    nlv, cash = gw.summary()
    acct = Account(nlv_eur=nlv, cash_eur=cash, positions=held, prices=price, eur_per_unit=fx)
    values = {s: port["positions"][s]["qty"] * price[s] * fx[s] for s in port.get("positions", {}) if s in price}
    orders = plan(target_weights(port, values), acct, r)
    stop = STOP_FILE.exists()
    room = r["max_giro"] * nlv - traded_today(account)
    for i, o in enumerate(orders):
        o["limit"] = round(o["ref"] * (1 + r["limite_pct"] if o["side"] == "BUY" else 1 - r["limite_pct"]), 2)
        o["coid"] = f"ogj-{aid}-{now:%Y%m%d%H%M}-{i}"[:40]
        why = ("PARAR" if stop else "bolsa fechada" if not market_open(o["symbol"], now)
               else "giro diário esgotado" if o["eur"] > room else None)
        if why or not send:
            o["resultado"] = {"nao_enviada": why or "só plano"}
            continue
        o["resultado"] = gw.place(o, confirm=confirm or not real)
        if o["resultado"].get("order_id"):
            room -= o["eur"]
    entry = {"at": now.isoformat(timespec="seconds"), "agente": aid, "conta": account[-3:],
             "tipo": "real" if real else "papel", "nlv_eur": round(nlv, 2), "enviado": send, "ordens": orders,
             "fora_do_agente": unknown}
    log(entry)
    return entry


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("acao", choices=["estado", "plano", "seguir"])
    ap.add_argument("--agente")
    ap.add_argument("--ciclo", type=int, default=0, help="seguir: repetir a cada N segundos (0: uma vez)")
    ap.add_argument("--confirmar", action="store_true", help="conta real: responder sim às perguntas da IBKR")
    ap.add_argument("--push", action="store_true", help="guardar o registo no repositório privado")
    a = ap.parse_args()
    gw = Gateway()
    if a.acao == "estado":
        st = gw.status()
        accts = gw.accounts() if st.get("authenticated") else []
        print(f"Sessão: {'sim' if st.get('authenticated') else 'não'} · contas: "
              + (", ".join(("papel " if is_paper(x) else "REAL ") + x for x in accts) or "—"))
        return 0
    r = rules()
    aid = a.agente or (r["agentes"] or [None])[0]
    if not aid:
        print("Diz qual agente: --agente <id>")
        return 1
    while True:
        try:
            out = cycle(gw, aid, r, send=a.acao == "seguir", confirm=a.confirmar)
        except Exception as e:  # noqa: BLE001 - the loop keeps going; the error is shown
            out = {"erro": repr(e)}
        if out.get("erro"):
            print(f"⚠️ {out['erro']}")
        else:
            print(f"{out['at']} · {out['tipo']} · {len(out['ordens'])} ordens")
            for o in out["ordens"]:
                print(f"  {o['side']:<4} {o['qty']:>9g} {o['symbol']:<10} ~{o['eur']:>9.2f} € · {o['resultado']}")
            if out["fora_do_agente"]:
                print(f"  (posições fora do agente, não mexidas: {', '.join(map(str, out['fora_do_agente']))})")
        if a.push:
            import save_state

            save_state.save("IBKR: registo", True, ["lab/ibkr"])
        if not a.ciclo or a.acao != "seguir":
            return 0
        try:
            gw.tickle()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(a.ciclo)


if __name__ == "__main__":
    sys.exit(main())
