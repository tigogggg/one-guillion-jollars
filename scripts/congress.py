"""Stock trades the members of the US House of Representatives disclose, posted when the report appears.

Under the STOCK Act, a member reports each trade (their own, their spouse's, a dependent child's) in a Periodic
Transaction Report (PTR) within 30 days of learning of it, and never more than 45 days after it. The House Clerk
lists every report in one index per year and posts each one as a PDF. Each run (with the news, 3x per weekday):

1. reads the index <YEAR>FD.zip and keeps the PTRs filed in the last RECENT_DAYS days not posted before
   (state/news/congress_seen.json, kept with the news in the Actions cache);
2. turns each PDF into text (pdftotext -layout, from poppler) and reads its rows: owner, stock and ticker,
   buy or sell, trade date, amount range, days it took to disclose;
3. posts the stock trades to the alerts channel, one card per run.

It is fast, not early: by the time a report is public the trade is days or weeks old. There is no inside
information here, only public filings, read quickly. Senate reports live in another system and are not read.

The PTR reader is adapted from day-trader-skill (trader-pelosi/ptr.py),
Copyright (c) 2026 Jake Schincariol, MIT License.

    uv run python scripts/congress.py --dry-run
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

import requests

REPO = Path(__file__).resolve().parents[1]
SEEN_PATH = REPO / "state" / "news" / "congress_seen.json"
INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
HEADERS = {"User-Agent": "OneGuillionJollars research github.com/tigogggg/one-guillion-jollars"}
RECENT_DAYS = 3  # reports filed this recently are posted once (an empty state never floods the channel)
LATE_DAYS = 45  # the legal limit to disclose a trade
SEEN_KEPT = 2000
MAX_ROWS = 25

OWNERS = {"SP": "cônjuge", "JT": "conjunta", "DC": "filho dependente", "": "o próprio"}
SIDES = {"P": ("🟢", "compra"), "S": ("🔴", "venda"), "S (partial)": ("🔴", "venda parcial"), "E": ("🔁", "troca")}


def iso(mdy: str) -> str | None:
    """'8/21/2026' or '08/21/2026' -> '2026-08-21'."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", mdy or "")
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year + 2000 if year < 100 else year, month, day).isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------


def parse_index(data: bytes, year: int) -> list[dict]:
    """The PTRs in a <YEAR>FD.zip (or its XML), newest first: doc_id, name, district, filed, url."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            data = z.read(next(n for n in z.namelist() if n.lower().endswith(".xml")))
    out = []
    for m in ElementTree.fromstring(data).iter("Member"):
        f = {c.tag: (c.text or "").strip() for c in m}
        if f.get("FilingType") != "P" or not f.get("DocID"):
            continue
        y = int(f.get("Year") or year)
        out.append({"doc_id": f["DocID"], "name": " ".join(x for x in (f.get("First"), f.get("Last")) if x),
                    "district": f.get("StateDst", ""), "filed": iso(f.get("FilingDate", "")),
                    "url": PTR_URL.format(year=y, doc=f["DocID"])})
    return sorted(out, key=lambda r: (r["filed"] or "", r["doc_id"]), reverse=True)


# --------------------------------------------------------------------------
# One report
# --------------------------------------------------------------------------

TXN = re.compile(
    r"^\s*(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s+(?P<type>S \(partial\)|P|S|E)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+(?P<amount>\S.*?)\s*$")
LABEL = re.compile(r"^\s*(F(?:ILING)?\s+S(?:TATUS)?|D(?:ESCRIPTION)?|S(?:UBHOLDING)?\s+O(?:F)?|L(?:OCATION)?)\s*:", re.I)
PAGE_HEADER = re.compile(r"^\s*(ID\s+Owner\s+Asset|Type\s+Date\b|Type$|Date$|Gains >$|\$200\?$)", re.I)
END = re.compile(r"^\s*(\* For the complete|I CERTIFY|Digitally Signed|Filing ID #)", re.I)


def parse_ptr_text(text: str, filed: str | None = None) -> list[dict]:
    """The rows of a PTR from `pdftotext -layout`: owner, asset, ticker, asset_type, type, date, amount, days."""
    rows, cur, in_asset, amount_open = [], None, False, False
    for raw in text.splitlines():
        line, s = raw.rstrip(), raw.strip()
        t = TXN.match(line)
        if t and iso(t.group("date")):
            if cur:
                rows.append(cur)
            cur = {"owner": t.group("owner") or "", "_asset": t.group("asset"), "type": t.group("type"),
                   "date": iso(t.group("date")), "amount": t.group("amount")}
            in_asset, amount_open = True, t.group("amount").endswith("-")
            continue
        if cur is None or not s or PAGE_HEADER.match(s):
            if cur is not None and not s and "[" in cur["_asset"]:
                in_asset = False
            continue
        if END.match(s) or LABEL.match(line):
            in_asset = False
            continue
        if in_asset:
            parts = re.split(r"\s{2,}", s)
            if amount_open and re.fullmatch(r"\$[\d,]+", parts[-1]):
                cur["amount"] += " " + parts.pop()
                amount_open = False
            if parts and parts[0]:
                cur["_asset"] += " " + " ".join(parts)
    if cur:
        rows.append(cur)
    for r in rows:
        asset = " ".join(r.pop("_asset").split())
        tick = re.findall(r"\(([A-Z][A-Z0-9.\-/]{0,9})\)", asset)
        code = re.findall(r"\[([A-Z]{2})\]", asset)
        r["ticker"] = tick[-1] if tick else None
        r["asset_type"] = code[-1] if code else None
        name = re.sub(r"\s*\[[A-Z]{2}\]", "", asset)
        r["asset"] = (re.sub(r"\s*\(%s\)" % re.escape(tick[-1]), "", name) if tick else name).strip(" -")
        r["amount"] = " ".join(r["amount"].split())
        r["days"] = (date.fromisoformat(filed) - date.fromisoformat(r["date"])).days if filed and r["date"] else None
    return rows


def pdf_text(pdf: bytes) -> str | None:
    """The PDF's text (pdftotext -layout), or None without poppler or for a scanned report (no text in it)."""
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf)
    try:
        out = subprocess.run([exe, "-layout", f.name, "-"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        Path(f.name).unlink(missing_ok=True)
    text = out.stdout.decode("utf-8", "replace") if out.returncode == 0 else ""
    return text if text.strip() else None


# --------------------------------------------------------------------------
# The card
# --------------------------------------------------------------------------


def describe(report: dict, row: dict) -> str:
    icon, side = SIDES.get(row["type"], ("•", row["type"]))
    what = f"**{row['ticker']}** ({row['asset'][:40]})" if row.get("ticker") else row["asset"][:50]
    owner = OWNERS.get(row["owner"], row["owner"])
    late = f" · declarado {row['days']} dias depois" + (" ⚠️ fora do prazo" if row["days"] > LATE_DAYS else "") \
        if row.get("days") is not None else ""
    return (f"{icon} {report['name']} ({report['district']}) · {side} {what} · {row['amount']} · {owner} · "
            f"negócio a {row['date'][8:10]}/{row['date'][5:7]}{late}")


def card(reports: list[dict]) -> dict | None:
    from discord_post import BLUE, embed

    lines, unread = [], []
    for r in reports:
        if r["rows"] is None:
            unread.append(f"[{r['name']}]({r['url']})")
            continue
        lines += [describe(r, row) for row in r["rows"] if row.get("asset_type") in (None, "ST", "OP")]
    if not lines and not unread:
        return None
    text = "\n".join(lines[:MAX_ROWS]) + (f"\n… e mais {len(lines) - MAX_ROWS}" if len(lines) > MAX_ROWS else "")
    if unread:
        text += "\n📄 Declarações digitalizadas, sem texto para ler: " + ", ".join(unread[:10])
    return embed("🏛️ Negócios declarados por membros do Congresso dos EUA", text.strip()[:4096], color=BLUE,
                 footer="Declarações públicas (STOCK Act) da Câmara dos Representantes. Chegam dias ou semanas depois do "
                        "negócio: não é informação privilegiada nem recomendação de compra.")


# --------------------------------------------------------------------------


def new_reports(today: date, seen: set[str]) -> list[dict]:
    """The PTRs filed in the last RECENT_DAYS days and not posted yet, read (rows None when unreadable)."""
    since = (today - timedelta(days=RECENT_DAYS)).isoformat()
    index = []
    for year in sorted({today.year, (today - timedelta(days=RECENT_DAYS)).year}):
        resp = requests.get(INDEX_URL.format(year=year), headers=HEADERS, timeout=60)
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        index += parse_index(resp.content, year)
    out = []
    for r in index:
        if (r["filed"] or "") < since or r["doc_id"] in seen:
            continue
        try:
            pdf = requests.get(r["url"], headers=HEADERS, timeout=60)
            pdf.raise_for_status()
            text = pdf_text(pdf.content)
        except requests.RequestException:
            continue  # tried again in the next run
        out.append({**r, "rows": parse_ptr_text(text, r["filed"]) if text else None})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print the card instead of posting it; state is saved")
    a = parser.parse_args()
    if not shutil.which("pdftotext"):
        print("::warning title=Congresso::sem pdftotext (poppler): os PDFs das declarações não podem ser lidos")
    seen_list = json.loads(SEEN_PATH.read_text()) if SEEN_PATH.exists() else []
    try:
        reports = new_reports(date.today(), set(seen_list))
    except (requests.RequestException, ElementTree.ParseError, StopIteration, zipfile.BadZipFile) as e:
        print(f"::warning title=Congresso::índice da Câmara indisponível: {e!r}")
        return 0
    rows = sum(len(r["rows"] or []) for r in reports)
    print(f"::notice title=Congresso::{len(reports)} declarações novas, {rows} negócios", flush=True)
    status = 0
    c = card(reports)
    if c:
        from discord_post import route, send

        status = send([c], dry_run=a.dry_run, **route("alertas"))
    if status == 0:  # posted (or nothing to post): these reports are done
        SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        SEEN_PATH.write_text(json.dumps((seen_list + [r["doc_id"] for r in reports])[-SEEN_KEPT:]))
    return status


if __name__ == "__main__":
    sys.exit(main())
