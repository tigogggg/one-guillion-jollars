import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import copy_alerts


def _mkt():
    return SimpleNamespace(stocks={"SAP.DE": {"name": "SAP"}}, etfs={}, as_of=date(2026, 11, 5))


def test_round_alerts_reach_only_the_followers_of_the_agent_that_traded(monkeypatch):
    sent = []
    monkeypatch.setattr(copy_alerts, "load_copiers", lambda: {
        "111": {"agente": "a1", "valor_eur": 5000}, "222": {"agente": "a2"}, "333": {"agente": "a1"}})
    monkeypatch.setattr(copy_alerts.discord_bot, "send_dm", lambda user, embeds, dry_run=False: sent.append((user, embeds)) or True)
    monkeypatch.setattr(copy_alerts.agents, "equity", lambda port, mkt: 10_000.0)
    cfg = {"id": "a1", "name": "Foguetão", "emoji": "🚀"}
    before = {"agents": {"a1": {"positions": {"REP.MC": {"qty": 10}}}}}
    moves = [(cfg, {"side": "compra", "symbol": "SAP.DE", "qty": 4, "price": 210.0, "eur": 1000, "reason": "rompimento"}),
             (cfg, {"side": "venda", "symbol": "REP.MC", "qty": 5, "price": 30.0, "eur": 150, "ret": 0.04, "reason": "alvo"})]
    assert copy_alerts.notify_round({}, before, {}, moves, _mkt(), "14:20") == 2
    assert sorted(u for u, _ in sent) == ["111", "333"]
    text = dict(sent)["111"][0]["description"]
    assert "Comprar** **SAP.DE** (SAP) · 10% da carteira do agente · ≈ 500 € dos teus 5 000 €" in text
    assert "Vender** **REP.MC** · 50% da posição" in text and "+4.0% no agente" in text
    assert "14:20" in dict(sent)["111"][0]["title"]
    assert "dos teus" not in dict(sent)["333"][0]["description"]  # no amount given: the share alone


def test_daily_alerts_carry_only_todays_orders(monkeypatch):
    sent = []
    monkeypatch.setattr(copy_alerts, "load_copiers", lambda: {"111": {"agente": "a1", "valor_eur": 2000}})
    monkeypatch.setattr(copy_alerts.discord_bot, "send_dm", lambda user, embeds, dry_run=False: sent.append(embeds) or True)
    monkeypatch.setattr(copy_alerts.agents, "equity", lambda port, mkt: 4_000.0)
    config = {"agents": [{"id": "a1", "name": "Analista", "emoji": "🧮"}]}
    state = {"agents": {"a1": {"pending": [
        {"side": "buy", "symbol": "SAP.DE", "eur": 400, "created": "2026-11-05", "stop": 190.0},
        {"side": "sell", "symbol": "OLD.DE", "created": "2026-11-04"}]}}}
    assert copy_alerts.notify_orders(config, state, _mkt()) == 1
    text = sent[0][0]["description"]
    assert "abertura** **SAP.DE**" in text and "≈ 200 € dos teus 2 000 €" in text and "OLD.DE" not in text


def test_no_followers_no_messages(monkeypatch):
    monkeypatch.setattr(copy_alerts, "load_copiers", dict)
    assert copy_alerts.notify_round({}, {}, {}, [({"id": "a1"}, {"side": "compra"})], _mkt(), "10:00") == 0
