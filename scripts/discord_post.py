"""Send Discord messages as embeds ("cards") with optional PNG attachments.

Used for the channel webhook (DISCORD_WEBHOOK_URL) and for editing a slash
command reply (interaction webhook). A dry run prints the text and saves the
images to reports/preview/.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
PREVIEW_DIR = REPO / "reports" / "preview"

ZONE_COLORS = {
    "Strong Bull": 0x57F287, "Strong": 0x57F287, "Bull": 0x57F287, "Healthy": 0x57F287,
    "Neutral": 0xFEE75C,
    "Cautious": 0xF0B232, "Weakening": 0xF0B232,
    "Bear": 0xED4245, "Critical": 0xED4245,
}
BLUE = 0x5865F2
GREEN = 0x57F287
RED = 0xED4245
DISCLAIMER = "Ferramenta de análise educativa, não é recomendação de compra."


CHANNELS_PATH = REPO / "discord" / "channels.json"
API = "https://discord.com/api/v10"


def route(kind: str) -> dict:
    """send()/post_message() arguments for the channel of `kind` ("mercado", "alertas", "agentes").

    Uses the bot to post into the channels created by discord_setup.py --canais
    (discord/channels.json); empty, i.e. the default webhook channel, until then.
    With DISCORD_TESTES set every kind goes to the 🧪-testes channel, and it is
    an error if that channel is missing rather than a post in the real one.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if os.environ.get("DISCORD_TESTES"):
        channel = json.loads(CHANNELS_PATH.read_text()).get("channels", {}).get("testes") if CHANNELS_PATH.exists() else None
        if not token or not channel:
            raise RuntimeError("DISCORD_TESTES definido mas sem canal 🧪-testes (discord_setup.py --canal-testes)")
        return {"url": f"{API}/channels/{channel}/messages", "headers": {"Authorization": f"Bot {token}"}}
    if not token or not CHANNELS_PATH.exists():
        return {}
    channel = json.loads(CHANNELS_PATH.read_text()).get("channels", {}).get(kind)
    if not channel:
        return {}
    return {"url": f"{API}/channels/{channel}/messages", "headers": {"Authorization": f"Bot {token}"}}


def embed(
    title: str,
    description: str = "",
    color: int = BLUE,
    image: str | None = None,
    footer: str = DISCLAIMER,
    fields: list[tuple[str, str]] | None = None,
) -> dict:
    """An embed dict; `image` names an attached file (attachment://name)."""
    e = {"title": title[:256], "description": description[:4096], "color": color}
    if fields:
        e["fields"] = [{"name": n[:256], "value": (v or "—")[:1024], "inline": False} for n, v in fields[:25]]
    if image:
        e["image"] = {"url": f"attachment://{image}"}
    if footer:
        e["footer"] = {"text": footer[:2048]}
    return e


def post_message(
    embeds: list[dict],
    files: dict[str, bytes] | None = None,
    dry_run: bool = False,
    url: str | None = None,
    method: str = "POST",
    content: str | None = None,
    thread_id: str | None = None,
    headers: dict | None = None,
) -> dict | None:
    """Post (or, with method=PATCH and an interaction url, edit) one message.

    Defaults to the channel webhook; `thread_id` posts inside a thread of that
    channel, `url` + `headers` send elsewhere (e.g. a DM with the bot token).
    Returns Discord's message object ({} on a dry run), or None on failure.
    """
    files = files or {}
    if dry_run:
        for e in embeds:
            print(f"### {e['title']}\n{e.get('description', '')}")
            for f in e.get("fields", []):
                print(f"**{f['name']}** {f['value']}")
            print(f"[{e.get('footer', {}).get('text', '')}]\n")
        if files:
            PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
            for name, data in files.items():
                (PREVIEW_DIR / name).write_bytes(data)
            print(f"(imagens em {PREVIEW_DIR}: {', '.join(files)})")
        return {}

    webhook = url is None
    url = url or os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("DISCORD_WEBHOOK_URL não definido; usa --dry-run para só ver a mensagem.", file=sys.stderr)
        return None
    params = {}
    if webhook:
        params["wait"] = "true"  # return the message, so callers get its id
        if thread_id:
            params["thread_id"] = thread_id

    payload = {"embeds": embeds[:10], "allowed_mentions": {"parse": []}}
    if content is not None:
        payload["content"] = content
    if files:
        payload["attachments"] = [{"id": i, "filename": name} for i, name in enumerate(files)]
        multipart = {f"files[{i}]": (name, data, "image/png") for i, (name, data) in enumerate(files.items())}
        resp = requests.request(method, url, params=params, headers=headers, data={"payload_json": json.dumps(payload)},
                                files=multipart, timeout=60)
    else:
        resp = requests.request(method, url, params=params, headers=headers, json=payload, timeout=60)
    if resp.status_code >= 300:
        print(f"Discord respondeu {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return None
    print("Mensagem enviada para o Discord.")
    try:
        return resp.json()
    except ValueError:
        return {}


def send(embeds: list[dict], files: dict[str, bytes] | None = None, dry_run: bool = False, **kwargs) -> int:
    """post_message, returning 0 on success and 1 on failure (exit status)."""
    return 0 if post_message(embeds, files, dry_run=dry_run, **kwargs) is not None else 1
