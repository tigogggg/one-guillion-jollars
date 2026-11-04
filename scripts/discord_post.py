"""Send Discord messages as embeds ("cards") with optional PNG attachments.

Used for the channel webhook (DISCORD_WEBHOOK_URL) and for editing a slash
command reply (interaction webhook). A dry run prints the text and saves the
images to reports/preview/.
"""

from __future__ import annotations

import json
import os
import sys
import time
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
MIRROR_PATH = REPO / "discord" / "mirror.json"  # a second server that gets a copy of every post
API = "https://discord.com/api/v10"


def detail_posts() -> bool:
    """Tiago (2026-11-04): 🤖-agentes only hears about the agents when a training made them better (pretrain's
    improvement card); rounds, the daily league, the jury, the labs and the reviews stay in the portal.
    OGJ_DISCORD_DETALHE=1 brings those posts back."""
    return os.environ.get("OGJ_DISCORD_DETALHE") == "1"


def route(kind: str) -> dict:
    """send()/post_message() arguments for the channel of `kind` ("mercado", "alertas", "agentes").

    Uses the bot to post into the channels created by discord_setup.py --canais
    (discord/channels.json); empty, i.e. the default webhook channel, until then.
    With DISCORD_TESTES set every kind goes to the 🧪-testes channel, and it is
    an error if that channel is missing rather than a post in the real one.
    The returned `kind` makes post_message copy the post to discord/mirror.json.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if os.environ.get("DISCORD_TESTES"):
        channel = json.loads(CHANNELS_PATH.read_text()).get("channels", {}).get("testes") if CHANNELS_PATH.exists() else None
        if not token or not channel:
            raise RuntimeError("DISCORD_TESTES definido mas sem canal 🧪-testes (discord_setup.py --canal-testes)")
        return {"url": f"{API}/channels/{channel}/messages", "headers": {"Authorization": f"Bot {token}"}, "kind": "testes"}
    if not token or not CHANNELS_PATH.exists():
        return {}
    channel = json.loads(CHANNELS_PATH.read_text()).get("channels", {}).get(kind)
    if not channel:
        return {}
    return {"url": f"{API}/channels/{channel}/messages", "headers": {"Authorization": f"Bot {token}"}, "kind": kind}


def mirror(kind: str, embeds: list[dict], files: dict[str, bytes], content: str | None) -> None:
    """Copy a post to the channel of the same kind in the mirror server; a failure there only warns."""
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token or not MIRROR_PATH.exists():
        return
    channel = json.loads(MIRROR_PATH.read_text()).get("channels", {}).get(kind)
    if not channel:
        return
    if post_message(embeds, files, url=f"{API}/channels/{channel}/messages", content=content,
                    headers={"Authorization": f"Bot {token}"}) is None:
        print(f"WARN: cópia para o servidor espelho (#{kind}) falhou", file=sys.stderr)


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
    kind: str | None = None,
    reply_to: str | None = None,
    buttons: list[tuple[str, str]] | None = None,
    components: list[dict] | None = None,
) -> dict | None:
    """Post (or, with method=PATCH and an interaction url, edit) one message.

    Defaults to the channel webhook; `thread_id` posts inside a thread of that
    channel, `url` + `headers` send elsewhere (e.g. a DM with the bot token).
    `kind` (set by route()) also copies the post to the mirror server, and
    `reply_to` answers an earlier message (a plain post if it is not there).
    `buttons` are (label, url) link buttons, sent with the bot or an
    interaction (a plain channel webhook cannot send them; give the embed a
    clickable title too). Returns Discord's message object ({} on a dry run), or None on failure.
    """
    files = files or {}
    if dry_run:
        for e in embeds:
            print(f"### {e['title']}\n{e.get('description', '')}")
            for f in e.get("fields", []):
                print(f"**{f['name']}** {f['value']}")
            print(f"[{e.get('footer', {}).get('text', '')}]\n")
        for label, link in buttons or []:
            print(f"[{label}]({link})")
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

    payload = {"embeds": fit(embeds[:10]), "allowed_mentions": {"parse": []}}
    if content is not None:
        payload["content"] = content
    if components and not webhook:
        payload["components"] = components
    elif buttons and not webhook:
        payload["components"] = [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": label[:80], "url": link} for label, link in buttons[:5]]}]
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
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
    if kind:
        mirror(kind, embeds, files, content)
    try:
        return resp.json()
    except ValueError:
        return {}


EMBED_TOTAL = 5800  # Discord refuses a message whose embeds add up to more than 6000 characters


def fit(embeds: list[dict], limit: int = EMBED_TOTAL) -> list[dict]:
    """Shorten the longest field values (then descriptions) until the embeds fit in one Discord message."""
    def size(e: dict) -> int:
        return (len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))
                + sum(len(f["name"]) + len(f["value"]) for f in e.get("fields", [])))
    embeds = [{**e, "fields": [dict(f) for f in e.get("fields", [])]} for e in embeds]
    while sum(size(e) for e in embeds) > limit:
        excess = sum(size(e) for e in embeds) - limit
        texts = [(len(f["value"]), f, "value") for e in embeds for f in e["fields"]] + [(len(e.get("description", "")), e, "description") for e in embeds]
        n, holder, key = max(texts, key=lambda t: t[0])
        if n <= 60:
            break
        holder[key] = holder[key][: max(40, n - excess - 2)].rstrip() + "…"
    return embeds


def delete_older(message: dict | None, title_prefix: str, scan: int = 100) -> int:
    """Delete the bot's earlier posts in the message's channel whose first embed title starts with title_prefix.

    Keeps `message` itself and pinned posts; only looks at the last `scan`
    messages. Needs the bot token (posts made through route()); a bot can
    always delete its own messages. Returns how many were deleted.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token or not message or not message.get("channel_id") or not message.get("author"):
        return 0
    headers = {"Authorization": f"Bot {token}"}
    channel, bot = message["channel_id"], message["author"]["id"]
    try:
        resp = requests.get(f"{API}/channels/{channel}/messages", params={"limit": scan}, headers=headers, timeout=30)
        resp.raise_for_status()
        history = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"WARN: não consegui ler o canal para limpar ({e})", file=sys.stderr)
        return 0
    old = [m for m in history
           if m["id"] != message["id"] and m.get("author", {}).get("id") == bot and not m.get("pinned")
           and m.get("embeds") and (m["embeds"][0].get("title") or "").startswith(title_prefix)]
    deleted = 0
    for m in old:
        for _ in range(3):  # Discord rate-limits deletes; wait as told and try again
            resp = requests.delete(f"{API}/channels/{channel}/messages/{m['id']}", headers=headers, timeout=30)
            if resp.status_code != 429:
                break
            time.sleep(float(resp.json().get("retry_after", 1)) + 0.1)
        if resp.status_code < 300:
            deleted += 1
        else:
            print(f"WARN: não consegui apagar a mensagem antiga {m['id']}: {resp.status_code}", file=sys.stderr)
    if deleted:
        print(f"Apagadas {deleted} publicações antigas de \"{title_prefix}\".")
    return deleted


def send(embeds: list[dict], files: dict[str, bytes] | None = None, dry_run: bool = False,
         replace: str | None = None, **kwargs) -> int:
    """post_message, returning 0 on success and 1 on failure (exit status).

    `replace`: a title prefix; once posted, the bot's older posts with that
    title in the same channel are deleted, so only the latest stays.
    """
    message = post_message(embeds, files, dry_run=dry_run, **kwargs)
    if message and replace and not dry_run:
        delete_older(message, replace)
    return 0 if message is not None else 1
