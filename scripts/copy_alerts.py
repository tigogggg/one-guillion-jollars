"""Private Discord messages to the members who follow an agent (profile → 👀 Seguir → Agente a seguir): every time
the agent buys or sells, in the same round it decides, so they can do the same in their own broker if they want.

The choice lives in the Worker's KV (copiar:<discord user id> = {"agente": id, "valor_eur": optional amount}); the
rounds (agents.py stops) send what the agents did this round, the daily run the orders it left for the next open.
Only counts go to the (public) Actions log.
"""

from __future__ import annotations

import sys

import discord_bot
from discord_post import GREEN, RED, embed

import agents

FOOTER = ("Sinal do agente que segues no teu perfil. Não é recomendação: decides tu e executas na tua corretora. "
          "Para deixar de receber: perfil → 👀 Seguir.")


def load_copiers() -> dict[str, dict]:
    """{discord user id: {"agente", "valor_eur"}} of everyone following an agent."""
    out = {}
    for user, v in discord_bot.load_prefix("copiar:").items():
        if isinstance(v, dict) and v.get("agente") and str(user).isdigit():
            out[str(user)] = v
    return out


def _name(mkt, symbol: str) -> str:
    info = (getattr(mkt, "stocks", {}) or {}).get(symbol) or (getattr(mkt, "etfs", {}) or {}).get(symbol) or {}
    return info.get("name", "")


def _money(x: float) -> str:
    return f"{x:,.0f} €".replace(",", " ")


def _scaled(share: float, valor) -> str:
    return f" · ≈ {_money(share * valor)} dos teus {_money(valor)}" if valor else ""


def trade_lines(mkt, before: dict | None, equity_before: float, trades: list[dict], valor) -> list[str]:
    """What the agent did this round, one line each, with the size as a share of its portfolio (and of the member's
    amount, when they gave one)."""
    lines = []
    for t in trades:
        if t.get("side") not in ("compra", "venda"):
            continue
        sym, name = t["symbol"], _name(mkt, t["symbol"])
        label = f"**{sym}**" + (f" ({name})" if name else "")
        cur = agents.currency_of(sym)
        if t["side"] == "compra":
            share = t["eur"] / equity_before if equity_before else 0.0
            lines.append(f"🟢 **Comprar** {label} · {share:.0%} da carteira do agente{_scaled(share, valor)}"
                         f" · a ≈ {t['price']:.2f} {cur}" + (f" · _{t['reason']}_" if t.get("reason") else ""))
        else:
            held = ((before or {}).get("positions", {}).get(sym) or {}).get("qty") or 0
            frac = min(t["qty"] / held, 1.0) if held else 1.0
            part = "tudo" if frac >= 0.99 else f"{frac:.0%} da posição"
            ret = f" · {t['ret']:+.1%} no agente" if t.get("ret") is not None else ""
            lines.append(f"🔴 **Vender** {label} · {part} · a ≈ {t['price']:.2f} {cur}{ret}"
                         + (f" · _{t['reason']}_" if t.get("reason") else ""))
    return lines


def order_lines(mkt, port: dict, equity_now: float, orders: list[dict], valor) -> list[str]:
    """The orders the daily run left for the next open."""
    lines = []
    for o in orders:
        sym, name = o["symbol"], _name(mkt, o["symbol"])
        label = f"**{sym}**" + (f" ({name})" if name else "")
        if o["side"] == "buy":
            share = (o.get("eur") or 0) / equity_now if equity_now else 0.0
            lines.append(f"🟢 **Comprar na abertura** {label} · {share:.0%} da carteira do agente{_scaled(share, valor)}"
                         + (f" · stop {o['stop']:.2f}" if o.get("stop") else "")
                         + (f" · _{o['reason']}_" if o.get("reason") else ""))
        else:
            frac = o.get("fraction", 1.0)
            part = "tudo" if frac >= 0.99 else f"{frac:.0%} da posição"
            lines.append(f"🔴 **Vender na abertura** {label} · {part}" + (f" · _{o['reason']}_" if o.get("reason") else ""))
    return lines


def _send(copiers: dict, per_agent: dict[str, tuple[dict, callable]], dry_run: bool) -> int:
    sent = 0
    for user, choice in copiers.items():
        entry = per_agent.get(choice["agente"])
        if not entry:
            continue
        title, build = entry
        try:
            valor = float(choice.get("valor_eur") or 0) or None
        except (TypeError, ValueError):
            valor = None
        lines = build(valor)
        if not lines:
            continue
        color = GREEN if all(line.startswith("🟢") for line in lines) else RED if all(
            line.startswith("🔴") for line in lines) else 0x5865F2
        card = embed(title, "\n".join(lines)[:4000], color=color, footer=FOOTER)
        if discord_bot.send_dm(user, [card], dry_run=dry_run):
            sent += 1
    return sent


def notify_round(config: dict, before: dict, state: dict, moves: list, mkt, clock: str, dry_run: bool = False) -> int:
    """After a round: one message per follower of each agent that traded. Returns how many were sent."""
    if not moves:
        return 0
    copiers = load_copiers()
    if not copiers:
        return 0
    by_agent: dict[str, tuple[dict, list]] = {}
    for cfg, t in moves:
        by_agent.setdefault(cfg["id"], (cfg, []))[1].append(t)
    per_agent = {}
    for aid, (cfg, trades) in by_agent.items():
        old = before.get("agents", {}).get(aid) or {}
        try:
            eq = agents.equity(old, mkt) if old else 0.0
        except Exception:  # noqa: BLE001 - a price missing: the size in euros of the agent's deposits
            eq = old.get("deposits") or 0.0
        per_agent[aid] = (f"{cfg['emoji']} {cfg['name']} decidiu agora ({clock})",
                          lambda valor, old=old, eq=eq, trades=trades: trade_lines(mkt, old, eq, trades, valor))
    try:
        n = _send(copiers, per_agent, dry_run)
    except Exception as e:  # noqa: BLE001 - the alerts never stop the round
        print(f"WARN: avisos a quem segue: {e!r}", file=sys.stderr)
        return 0
    print(f"Avisos a quem segue os agentes: {n} mensagens", flush=True)
    return n


def notify_orders(config: dict, state: dict, mkt, dry_run: bool = False) -> int:
    """After the daily run: the orders each followed agent created today, for the next open."""
    copiers = load_copiers()
    if not copiers:
        return 0
    wanted = {c["agente"] for c in copiers.values()}
    today = mkt.as_of.isoformat()
    per_agent = {}
    for cfg in config["agents"]:
        port = state["agents"].get(cfg["id"])
        if cfg["id"] not in wanted or not port:
            continue
        orders = [o for o in port.get("pending", []) if o.get("created") == today]
        if not orders:
            continue
        eq = agents.equity(port, mkt)
        per_agent[cfg["id"]] = (f"{cfg['emoji']} {cfg['name']}: ordens para a próxima abertura",
                                lambda valor, port=port, eq=eq, orders=orders: order_lines(mkt, port, eq, orders, valor))
    try:
        n = _send(copiers, per_agent, dry_run)
    except Exception as e:  # noqa: BLE001
        print(f"WARN: avisos a quem segue: {e!r}", file=sys.stderr)
        return 0
    print(f"Avisos a quem segue os agentes: {n} mensagens", flush=True)
    return n
