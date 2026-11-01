"""Off-site copy of the members' data: KV keys and the D1 portfolios, as a gzip JSON file.

The Worker already keeps 14 daily copies inside D1 and D1 keeps 30 days of point-in-time
history; this copy lives outside Cloudflare (a GitHub Actions artifact of this private repo,
kept 30 days). With BACKUP_KEY set, the file is encrypted (AES-256, openssl) before upload.

    uv run python scripts/backup.py out/backup.json.gz
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from discord_bot import KV_TITLE

PREFIXES = ["portfolio:", "portfolio-user:", "login-google:", "follows:", "schedule", "portal:token", "discord:ids"]
D1_NAME = "ogj"


def kv_dump(base: str, headers: dict) -> dict:
    spaces = requests.get(base, headers=headers, params={"per_page": 100}, timeout=30).json()["result"]
    ns = next(s["id"] for s in spaces if s["title"] == KV_TITLE)
    out = {}
    for prefix in PREFIXES:
        cursor = None
        while True:
            params = {"prefix": prefix, "limit": 1000, **({"cursor": cursor} if cursor else {})}
            page = requests.get(f"{base}/{ns}/keys", headers=headers, params=params, timeout=60).json()
            for k in page["result"]:
                value = requests.get(f"{base}/{ns}/values/{quote(k['name'], safe='')}", headers=headers, timeout=60)
                if value.status_code == 200:
                    out[k["name"]] = value.text
            cursor = (page.get("result_info") or {}).get("cursor")
            if not cursor:
                break
    return out


def d1_dump(account: str, headers: dict) -> dict | None:
    api = f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database"
    found = requests.get(api, headers=headers, params={"name": D1_NAME}, timeout=30).json()
    db = next((d["uuid"] for d in found.get("result") or [] if d.get("name") == D1_NAME), None)
    if not db:
        print(f"::warning title=Backup::Sem acesso à base D1 ({(found.get('errors') or [{}])[0].get('message', 'não existe')})")
        return None
    out = {}
    for table, sql in [("portfolios", "SELECT token, owner, data, version, updated FROM portfolios"),
                       ("versions", "SELECT token, version, label, at FROM versions")]:
        resp = requests.post(f"{api}/{db}/query", headers=headers, json={"sql": sql}, timeout=120).json()
        if not resp.get("success"):
            print(f"::warning title=Backup::D1 {table}: {resp.get('errors')}")
            continue
        out[table] = resp["result"][0]["results"]
    return out


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "backup.json.gz")
    token, account = os.environ.get("CLOUDFLARE_API_TOKEN"), os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account:
        print("::error::Faltam CLOUDFLARE_API_TOKEN e CLOUDFLARE_ACCOUNT_ID")
        return 1
    headers = {"Authorization": f"Bearer {token}"}
    base = f"https://api.cloudflare.com/client/v4/accounts/{account}/storage/kv/namespaces"
    kv = kv_dump(base, headers)
    d1 = d1_dump(account, headers)
    data = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kv": kv, "d1": d1}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(gzip.compress(json.dumps(data, ensure_ascii=False).encode()))
    n = len((d1 or {}).get("portfolios", [])) or sum(k.startswith("portfolio:") for k in kv)
    print(f"::notice title=Backup::{len(kv)} chaves do KV, {n} carteiras, {target.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
