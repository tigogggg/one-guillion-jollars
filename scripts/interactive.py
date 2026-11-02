"""Interactive version of a lines chart: the data goes to the Worker's KV and the Worker serves it as a page.

publish() stores {title, series, references, highlight} under chart:<random id>
(kept CHART_DAYS days) and returns https://<worker>.<subdomain>.workers.dev/g/<id>;
discord-bot/worker.js draws it with Plotly: hover values, zoom, and agents
switched on and off in the legend. Without Cloudflare credentials it returns
None and the posts go out with the static chart only.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

import discord_bot

REPO = Path(__file__).resolve().parents[1]
CHART_DAYS = 120
BUTTON = "📈 Ver gráfico interativo"


def worker_url() -> str | None:
    """https://<worker name>.<account subdomain>.workers.dev, from wrangler.toml and the Cloudflare API."""
    token, account = os.environ.get("CLOUDFLARE_API_TOKEN"), os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account:
        return None
    name = re.search(r'^name\s*=\s*"([^"]+)"', (REPO / "discord-bot" / "wrangler.toml").read_text(), re.M)
    try:
        resp = requests.get(f"https://api.cloudflare.com/client/v4/accounts/{account}/workers/subdomain",
                            headers={"Authorization": f"Bearer {token}"}, timeout=30)
        sub = resp.json()["result"]["subdomain"]
    except (requests.RequestException, KeyError, TypeError, ValueError) as e:
        print(f"WARN: subdomínio do Worker: {e}", file=sys.stderr)
        return None
    return f"https://{name.group(1)}.{sub}.workers.dev" if name and sub else None


def _series(series: dict[str, list[tuple]]) -> list[dict]:
    return [{"name": name, "x": [str(d)[:10] for d, _ in points], "y": [round(float(v), 2) for _, v in points]}
            for name, points in series.items() if points]


def publish(title: str, series: dict[str, list[tuple]], references: dict[str, list[tuple]] | None = None,
            highlight: set[str] | None = None, subtitle: str = "", agents: list[dict] | None = None,
            kind: str | None = None) -> str | None:
    """Store the chart and return its page URL, or None (no credentials, or Cloudflare unreachable).

    `agents`: one dict per agent (agents.agent_details or pretrain's), shown as
    a detailed card per agent under the chart. `kind` ("liga", "pretreino")
    lets an older page point to the newest one of the same kind.
    """
    base = worker_url()
    if not base:
        return None
    chart_id = secrets.token_hex(12)
    data = {
        "title": title,
        "subtitle": subtitle,
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "series": _series(series),
        "references": _series(references or {}),
        "highlight": sorted(highlight or []),
        "agents": agents or [],
        "kind": kind,
    }
    if not discord_bot.kv_put(f"chart:{chart_id}", data, ttl_days=CHART_DAYS):
        return None
    if kind:
        discord_bot.kv_put(f"chart-latest:{kind}", chart_id)
    url = f"{base}/g/{chart_id}"
    if os.environ.get("GITHUB_ACTIONS"):  # visible on the run page (private repo)
        print(f"::notice title=Gráfico interativo::{url}")
    return url


def portal_url() -> str | None:
    """The shared portal's private link (/p/<token>), creating the token on first use; None without Cloudflare."""
    base = worker_url()
    if not base:
        return None
    token = discord_bot.kv_get("portal:token")
    if not token:
        token = secrets.token_hex(12)
        if not discord_bot.kv_put("portal:token", token):
            return None
    return f"{base}/p/{token}"


def publish_section(name: str, data) -> bool:
    """One section of the portal (news, market), read by the Worker when the portal opens. portal:version changes too,
    so open portal pages know to refresh themselves."""
    ok = discord_bot.kv_put(f"portal:{name}", data)
    if ok:
        from datetime import datetime, timezone

        discord_bot.kv_put("portal:version", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return ok


def leaders(series: dict[str, list[tuple]], n: int = 5, always: tuple[str, ...] = ("Indexador",)) -> set[str]:
    """The n series that end highest, plus the benchmark: the ones drawn in colour."""
    last = {name: points[-1][1] for name, points in series.items() if points}
    top = sorted(last, key=last.get, reverse=True)[:n]
    return set(top) | {name for name in always if name in last}
