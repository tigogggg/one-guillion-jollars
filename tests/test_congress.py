"""The PTR fixture is the text of a real House filing (DocID 20035590), as saved by day-trader-skill (MIT)."""
import io
import sys
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import congress  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "ptr-20035590.txt"


def test_a_house_ptr_becomes_rows_with_ticker_side_amount_and_delay():
    rows = congress.parse_ptr_text(FIXTURE.read_text(encoding="utf-8"), filed="2026-10-21")
    assert [(r["owner"], r["ticker"], r["type"], r["date"], r["amount"]) for r in rows] == [
        ("JT", "AMAT", "S", "2026-09-15", "$1,001 - $15,000"),
        ("JT", "MCD", "S (partial)", "2026-09-25", "$1,001 - $15,000"),
    ]
    assert rows[0]["asset"].startswith("Applied Materials") and rows[0]["asset_type"] == "ST" and rows[0]["days"] == 36


def test_the_index_keeps_only_transaction_reports():
    xml = b"""<FinancialDisclosure>
      <Member><First>Nancy</First><Last>Pelosi</Last><StateDst>CA11</StateDst><FilingType>P</FilingType>
              <Year>2026</Year><FilingDate>10/21/2026</FilingDate><DocID>20035590</DocID></Member>
      <Member><First>A</First><Last>B</Last><StateDst>NY01</StateDst><FilingType>O</FilingType>
              <Year>2026</Year><FilingDate>10/22/2026</FilingDate><DocID>10000001</DocID></Member>
    </FinancialDisclosure>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("2026FD.xml", xml)
    index = congress.parse_index(buf.getvalue(), 2026)
    assert [(r["doc_id"], r["name"], r["filed"]) for r in index] == [("20035590", "Nancy Pelosi", "2026-10-21")]
    assert index[0]["url"].endswith("/ptr-pdfs/2026/20035590.pdf")


def test_the_card_lists_the_trades_and_the_unreadable_reports():
    rows = congress.parse_ptr_text(FIXTURE.read_text(encoding="utf-8"), filed="2026-10-21")
    reports = [{"name": "Josh Gottheimer", "district": "NJ05", "url": "u1", "rows": rows},
               {"name": "Outro Membro", "district": "TX02", "url": "u2", "rows": None}]
    c = congress.card(reports)
    assert "🔴 Josh Gottheimer (NJ05) · venda **AMAT**" in c["description"] and "declarado 36 dias depois" in c["description"]
    assert "digitalizadas" in c["description"] and "[Outro Membro](u2)" in c["description"]
    assert congress.card([]) is None
