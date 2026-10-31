import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import alert_followups  # noqa: E402
import discord_post  # noqa: E402
import health  # noqa: E402


def test_job_lines_flag_missing_and_failed_runs():
    ok = {"status": "completed", "conclusion": "success", "html_url": "u1"}
    bad = {"status": "completed", "conclusion": "failure", "html_url": "u2"}
    assert health.job_line("Dicas", [ok], 1) == (True, "✅ Dicas: 1× ok")
    assert health.job_line("Dicas", [], 1) == (False, "⚠️ Dicas: não correu hoje")
    assert health.job_line("Alertas", [ok, bad], 1) == (False, "❌ Alertas: 1 de 2 falharam · [ver](u2)")
    assert not health.job_line("Dicas", None, 1)[0]


def test_friday_data_is_fresh_on_monday_but_not_on_wednesday():
    friday = date(2026, 10, 30)
    assert health.data_line("B", friday, date(2026, 11, 2))[0]  # Monday: 1 weekday later
    assert health.data_line("B", friday, date(2026, 11, 3))[0]  # Tuesday: 2
    assert health.data_line("B", friday, date(2026, 11, 4)) == (False, "⚠️ B: dados de 30/10, 3 dias úteis de atraso")


def test_card_colour_and_weekend_note():
    card = health.build_card(date(2026, 10, 31), [], [(True, "✅ B: dados de 30/10")])
    assert card["title"].endswith("tudo a funcionar") and card["color"] == discord_post.GREEN
    assert "Fim de semana" in card["description"]
    card = health.build_card(date(2026, 11, 2), [(False, "⚠️ Dicas: não correu hoje")], [(True, "✅ B")])
    assert card["title"].endswith("1 problema(s)") and card["color"] == health.ORANGE


def test_health_never_posts_without_its_channel(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["health.py"])
    monkeypatch.setattr(health, "runs_today", lambda wf, today: [])
    monkeypatch.setattr(health, "last_data_date", lambda url, col: date.today())
    monkeypatch.setattr(discord_post, "CHANNELS_PATH", tmp_path / "channels.json")
    (tmp_path / "channels.json").write_text(json.dumps({"channels": {"alertas": "11"}}))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.delenv("DISCORD_TESTES", raising=False)
    monkeypatch.setattr(discord_post.requests, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert health.main() == 0
    assert "Estado do bot" in capsys.readouterr().out


def prices(start: str, closes: list[float], lows: list[float] | None = None) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(closes))
    lows = lows or [c - 1 for c in closes]
    return pd.DataFrame({"Close": closes, "High": [c + 2 for c in closes], "Low": lows}, index=idx)


def test_followups_are_due_once_per_milestone_and_skip_old_backlog():
    alert = {"date": "2026-10-01", "symbol": "AAA", "price": 100.0, "stop": 95.0, "message_id": "M1", "channel_id": "11"}
    # Alert on Thu 1 Oct; sessions from Fri 2 Oct. 6 sessions after: the 5-session milestone is due.
    df = prices("2026-10-01", [100, 101, 102, 103, 104, 110, 108])
    items = alert_followups.due([alert], {"AAA": df})
    assert [(m, round(r["move"], 4), round(r["best"], 4), r["stopped"]) for _, m, r in items] == [(5, 0.10, 0.12, None)]

    alert["followups"] = [5]
    assert alert_followups.due([alert], {"AAA": df}) == []  # already posted
    old = dict(alert, followups=[])
    long = prices("2026-10-01", [100] * 10)  # 9 sessions after: 5 is more than 3 sessions late
    assert alert_followups.due([old], {"AAA": long}) == []


def test_stop_hit_and_one_reply_per_alert_message():
    a = {"date": "2026-10-01", "symbol": "AAA", "price": 100.0, "stop": 95.0, "message_id": "M1"}
    b = {"date": "2026-10-01", "symbol": "BBB", "price": 50.0, "stop": 48.0, "message_id": "M1"}
    dfa = prices("2026-10-01", [100, 99, 97, 96, 98, 97], lows=[99, 98, 94, 95, 97, 96])
    dfb = prices("2026-10-01", [50, 51, 52, 53, 54, 55])
    items = alert_followups.due([a, b], {"AAA": dfa, "BBB": dfb})
    replies = alert_followups.cards(items)
    assert [m for m, _ in replies] == ["M1"]
    text = replies[0][1]["description"]
    assert "**AAA** · 5 sessões depois: -3.0% (melhor +1.0%) · 🛑 stop atingido a 05/10" in text
    assert "**BBB** · 5 sessões depois: +10.0%" in text and "🟢 stop intacto" in text


def test_reply_to_sets_the_message_reference(monkeypatch):
    sent = {}

    def fake_request(method, url, **kw):
        sent.update(kw["json"])
        return type("R", (), {"status_code": 200, "json": lambda self: {"id": "N"}, "text": ""})()
    monkeypatch.setattr(discord_post.requests, "request", fake_request)
    discord_post.post_message([{"title": "x"}], url="https://discord.com/api/v10/channels/11/messages", reply_to="M1")
    assert sent["message_reference"] == {"message_id": "M1", "fail_if_not_exists": False}
