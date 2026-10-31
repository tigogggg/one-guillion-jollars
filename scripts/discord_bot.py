"""Discord actions that need the bot user (DISCORD_BOT_TOKEN) rather than the webhook.

The bot must be a member of the server (installed with the "bot" scope) to
create threads, add and read reactions, and send DMs. Every function degrades
to doing nothing when the token is missing or Discord refuses, so the posts
themselves never depend on it.

Also reads the /seguir lists, which the Cloudflare Worker keeps in its KV
namespace (keys follows:<user id>), through the Cloudflare API.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests

from discord_post import post_message

API = "https://discord.com/api/v10"
# Inside the watchlist folder, so the Actions cache carries it to the day's later runs.
THREAD_PATH = Path(__file__).resolve().parents[1] / "state" / "watchlist" / "thread.json"
KV_TITLE = "coutotrading-config"
VOTES = ("👍", "👎")


def _token() -> str | None:
    return os.environ.get("DISCORD_BOT_TOKEN")


def _bot(method: str, path: str, **kwargs) -> requests.Response | None:
    token = _token()
    if not token:
        return None
    try:
        resp = requests.request(method, API + path, headers={"Authorization": f"Bot {token}"}, timeout=30, **kwargs)
    except requests.RequestException as e:
        print(f"WARN: bot {method} {path}: {e}", file=sys.stderr)
        return None
    if resp.status_code >= 300:
        print(f"WARN: bot {method} {path}: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return None
    return resp


# --------------------------------------------------------------------------
# Daily thread
# --------------------------------------------------------------------------


def start_daily_thread(message: dict, name: str, today: date) -> str | None:
    """Open a thread on the day's first post and remember it for the day's other posts."""
    if not message.get("id"):
        return None
    resp = _bot("POST", f"/channels/{message['channel_id']}/messages/{message['id']}/threads",
                json={"name": name[:100], "auto_archive_duration": 1440})
    if resp is None:
        return None
    thread_id = resp.json()["id"]
    THREAD_PATH.parent.mkdir(parents=True, exist_ok=True)
    THREAD_PATH.write_text(json.dumps({"date": today.isoformat(), "thread_id": thread_id}))
    return thread_id


def todays_thread(today: date | None = None) -> str | None:
    """The thread opened by today's tips, if any (shared through the watchlist cache)."""
    if not THREAD_PATH.exists():
        return None
    data = json.loads(THREAD_PATH.read_text())
    return data["thread_id"] if data.get("date") == (today or date.today()).isoformat() else None


# --------------------------------------------------------------------------
# Reactions (votes on alerts)
# --------------------------------------------------------------------------


def add_votes(message: dict) -> None:
    for emoji in VOTES:
        _bot("PUT", f"/channels/{message['channel_id']}/messages/{message['id']}/reactions/{quote(emoji)}/@me")


def count_votes(channel_id: str, message_id: str) -> dict[str, int] | None:
    """{"👍": n, "👎": n} from people (the bot's own seed reactions are not counted)."""
    resp = _bot("GET", f"/channels/{channel_id}/messages/{message_id}")
    if resp is None:
        return None
    counts = {e: 0 for e in VOTES}
    for r in resp.json().get("reactions", []):
        emoji = r.get("emoji", {}).get("name")
        if emoji in counts:
            counts[emoji] = r.get("count", 0) - (1 if r.get("me") else 0)
    return counts


# --------------------------------------------------------------------------
# Direct messages
# --------------------------------------------------------------------------


def send_dm(user_id: str, embeds: list[dict], files: dict[str, bytes] | None = None, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"--- DM para {user_id}")
        return post_message(embeds, files, dry_run=True) is not None
    resp = _bot("POST", "/users/@me/channels", json={"recipient_id": user_id})
    if resp is None:
        return False
    channel = resp.json()["id"]
    sent = post_message(embeds, files, url=f"{API}/channels/{channel}/messages",
                        headers={"Authorization": f"Bot {_token()}"})
    return sent is not None


# --------------------------------------------------------------------------
# /seguir lists from the Worker's KV
# --------------------------------------------------------------------------


def kv_get(key: str):
    """One JSON value from the Worker's KV (e.g. "schedule"), or None without credentials or on error."""
    token, account = os.environ.get("CLOUDFLARE_API_TOKEN"), os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account:
        return None
    base = f"https://api.cloudflare.com/client/v4/accounts/{account}/storage/kv/namespaces"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        spaces = requests.get(base, headers=headers, params={"per_page": 100}, timeout=30).json()["result"]
        ns = next(s["id"] for s in spaces if s["title"] == KV_TITLE)
        value = requests.get(f"{base}/{ns}/values/{quote(key, safe='')}", headers=headers, timeout=30)
        return json.loads(value.text) if value.status_code == 200 else None
    except (requests.RequestException, KeyError, StopIteration, ValueError) as e:
        print(f"WARN: KV {key}: {e}", file=sys.stderr)
        return None


def load_follows() -> dict[str, list[str]]:
    """{discord user id: [tickers]}; empty without Cloudflare credentials."""
    token, account = os.environ.get("CLOUDFLARE_API_TOKEN"), os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account:
        return {}
    base = f"https://api.cloudflare.com/client/v4/accounts/{account}/storage/kv/namespaces"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        spaces = requests.get(base, headers=headers, params={"per_page": 100}, timeout=30).json()["result"]
        ns = next(s["id"] for s in spaces if s["title"] == KV_TITLE)
        keys = requests.get(f"{base}/{ns}/keys", headers=headers, params={"prefix": "follows:"}, timeout=30).json()["result"]
        out = {}
        for k in keys:
            value = requests.get(f"{base}/{ns}/values/{quote(k['name'], safe='')}", headers=headers, timeout=30)
            if value.status_code == 200:
                out[k["name"].split(":", 1)[1]] = json.loads(value.text)
        return out
    except (requests.RequestException, KeyError, StopIteration, ValueError) as e:
        print(f"WARN: listas /seguir: {e}", file=sys.stderr)
        return {}
