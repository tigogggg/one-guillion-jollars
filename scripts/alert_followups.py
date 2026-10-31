"""Follow-up of each opportunity alert, 5 and 20 sessions later (STAND-BY).

Replies to the original alert message in 🚨-alertas-e-notícias with how each
stock did since the alert: change, best point and whether the stop was hit.
Reads and marks state/history/alerts.json (shared through the Actions cache),
so each milestone is posted once. Milestones more than a few sessions old are
skipped, so switching this on does not post a backlog.

Not scheduled by the Worker yet; start alert-followups.yml by hand to try it.

    uv run python scripts/alert_followups.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

import pandas as pd

from discord_post import GREEN, RED, embed, post_message, route
from opportunity_alerts import HISTORY_PATH
from rankings import download_history

MILESTONES = (5, 20)  # trading sessions after the alert
LATE = 3  # still post a milestone up to this many sessions late (a missed day)


def due(history: list[dict], prices: dict[str, pd.DataFrame]) -> list[tuple[dict, int, dict]]:
    """(alert, milestone, result) for every milestone reached and not posted yet."""
    out = []
    for a in history:
        df = prices.get(a["symbol"])
        if df is None or not a.get("message_id"):
            continue
        after = df[df.index.date > date.fromisoformat(a["date"])]
        for m in MILESTONES:
            if m in a.get("followups", []) or not m <= len(after) < m + LATE:
                continue
            window = after.iloc[:m]
            hit = window[window["Low"] <= a["stop"]]
            out.append((a, m, {
                "move": float(window["Close"].iloc[-1]) / a["price"] - 1,
                "best": float(window["High"].max()) / a["price"] - 1,
                "stopped": hit.index[0].date() if len(hit) else None,
            }))
    return out


def line(a: dict, m: int, r: dict) -> str:
    status = f"🛑 stop atingido a {r['stopped']:%d/%m}" if r["stopped"] else ("🟢" if r["move"] > 0 else "🔴") + " stop intacto"
    return (f"**{a['symbol']}** · {m} sessões depois: {r['move'] * 100:+.1f}% "
            f"(melhor {r['best'] * 100:+.1f}%) · {status}")


def cards(items: list[tuple[dict, int, dict]]) -> list[tuple[str, dict]]:
    """One reply card per original alert message."""
    by_message: dict[str, list] = {}
    for a, m, r in items:
        by_message.setdefault(a["message_id"], []).append((a, m, r))
    out = []
    for message_id, group in by_message.items():
        up = sum(r["move"] > 0 and not r["stopped"] for _, _, r in group)
        out.append((message_id, embed(
            f"🔁 Como correu o alerta de {group[0][0]['date'][8:]}/{group[0][0]['date'][5:7]}",
            "\n".join(line(a, m, r) for a, m, r in group),
            color=GREEN if up * 2 >= len(group) else RED,
            footer="Fecho vs preço no momento do alerta; stop pelos mínimos diários. Resultados passados não garantem nada.",
        )))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print the replies instead of posting")
    args = parser.parse_args()

    history = json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else []
    if not history:
        print("Sem histórico de alertas.", file=sys.stderr)
        return 0
    prices = download_history(sorted({a["symbol"] for a in history}), period="6mo")
    items = due(history, prices)
    print(f"{len(history)} alertas no histórico, {len(items)} acompanhamentos a publicar", file=sys.stderr)
    testing = bool(os.environ.get("DISCORD_TESTES"))
    where = {} if args.dry_run else route("alertas")
    for message_id, card in cards(items):
        if post_message([card], dry_run=args.dry_run, reply_to=message_id, **where) is None:
            return 1
    if items and not args.dry_run and not testing:  # a test run must not use up the real follow-ups
        for a, m, _ in items:
            a.setdefault("followups", []).append(m)
        HISTORY_PATH.write_text(json.dumps(history, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
