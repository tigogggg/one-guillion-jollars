import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ibkr

R = {**ibkr.DEFAULTS, "capital_eur": 5000.0, "agentes": ["a1"]}


def test_listing_maps_yahoo_symbols_to_ibkr_exchanges():
    assert ibkr.listing("SAP.DE") == ("SAP", "IBIS", "EUR")
    assert ibkr.listing("REP.MC") == ("REP", "BM", "EUR")
    assert ibkr.listing("BRK-B") == ("BRK B", "SMART", "USD")
    assert ibkr.listing("ULVR.L") == ("ULVR", "LSE", "GBP")


def test_target_weights_include_the_orders_for_the_next_open():
    port = {"cash": 1000.0, "positions": {"A.DE": {}, "B.DE": {}},
            "pending": [{"side": "sell", "symbol": "B.DE", "fraction": 1.0}, {"side": "buy", "symbol": "C.DE", "eur": 1500}]}
    w = ibkr.target_weights(port, {"A.DE": 2000.0, "B.DE": 1000.0})
    assert w == {"A.DE": 0.5, "C.DE": 0.375}  # B sold, its cash and some of the rest buy C; 500 € stays in cash


def test_plan_buys_whole_shares_sells_exits_and_ignores_small_gaps():
    acct = ibkr.Account(nlv_eur=5000, cash_eur=2000, positions={"OLD.DE": 10, "A.DE": 10},
                        prices={"OLD.DE": 30.0, "A.DE": 100.0, "B.DE": 333.0}, eur_per_unit={"OLD.DE": 1, "A.DE": 1, "B.DE": 1})
    orders = ibkr.plan({"A.DE": 0.205, "B.DE": 0.4}, acct, R)
    by = {o["symbol"]: o for o in orders}
    assert by["OLD.DE"]["side"] == "SELL" and by["OLD.DE"]["qty"] == 10  # no longer in the agent: sold whole
    assert "A.DE" not in by  # 1025 € wanted, 1000 € held: under the 100 € floor
    assert by["B.DE"]["side"] == "BUY" and by["B.DE"]["qty"] == 6  # 2000 € / 333 = 6.006 -> 6 whole shares
    assert orders[0]["side"] == "SELL"


def test_plan_buys_only_what_the_cash_allows():
    acct = ibkr.Account(nlv_eur=1000, cash_eur=1000, prices={"X.DE": 10.0}, eur_per_unit={"X.DE": 1.0})
    (o,) = ibkr.plan({"X.DE": 1.0}, acct, R)
    cost = o["eur"] * (1 + ibkr.slippage("X.DE")) + ibkr.commission_eur("X.DE", o["eur"], o["qty"], 1.0)
    assert o["qty"] == int(o["qty"]) and cost <= 1000 and o["qty"] >= 97


def test_virtual_fill_pays_ibkr_costs():
    v = {"cash_eur": 1000.0, "positions": {}, "custos_eur": 0.0, "ordens": 0}
    acct = ibkr.Account(nlv_eur=1000, cash_eur=1000, prices={"X.DE": 10.0}, eur_per_unit={"X.DE": 1.0})
    (done,) = ibkr.fill_virtual(v, acct, [{"symbol": "X.DE", "side": "BUY", "qty": 50, "ref": 10.0, "eur": 500}])
    assert done["comissao_eur"] == 1.25  # IBKR Europe: 0.06%, at least 1.25 €
    assert v["positions"] == {"X.DE": 50} and v["cash_eur"] < 500 and v["custos_eur"] > 1.25


class FakeGateway:
    def __init__(self, account="DU123456", authenticated=True, positions=()):
        self.account, self._auth, self._pos, self.sent = None, authenticated, list(positions), []
        self._acct, self._conids = account, {"A.DE": 1, "B.DE": 2}

    def status(self):
        return {"authenticated": self._auth}

    def accounts(self):
        return [self._acct]

    def use(self, a):
        self.account = a

    def conid(self, s):
        return self._conids.get(s)

    def positions(self):
        return self._pos

    def summary(self):
        return 5000.0, 5000.0

    def place(self, order, confirm):
        self.sent.append((order, confirm))
        return {"order_id": str(len(self.sent))}


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(ibkr, "LAB", tmp_path)
    monkeypatch.setattr(ibkr, "STOP_FILE", tmp_path / "PARAR")
    monkeypatch.setattr(ibkr.agents, "load_state", lambda: {"agents": {"a1": {
        "cash": 0.0, "positions": {"A.DE": {"qty": 10}}, "pending": []}}})
    return tmp_path


OPEN = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)  # 11:00 in Frankfurt
PRICES = ({"A.DE": 100.0}, {"A.DE": 1.0})


def test_cycle_sends_on_a_paper_account_while_the_exchange_is_open(lab):
    gw = FakeGateway()
    out = ibkr.cycle(gw, "a1", R, send=True, confirm=False, now=OPEN, prices=PRICES)
    assert out["tipo"] == "papel" and [o["symbol"] for o in out["ordens"]] == ["A.DE"]
    order, confirm = gw.sent[0]
    assert order["side"] == "BUY" and order["limit"] == 100.5 and confirm  # paper: IBKR's questions answered yes
    assert (lab / f"registo-{datetime.now(UTC):%Y-%m}.jsonl").exists()


def test_cycle_never_touches_a_real_account_without_both_switches(lab, monkeypatch):
    monkeypatch.delenv("IBKR_REAL", raising=False)
    gw = FakeGateway(account="U7654321")
    assert "conta real" in ibkr.cycle(gw, "a1", R, send=True, confirm=False, now=OPEN, prices=PRICES)["erro"]
    monkeypatch.setenv("IBKR_REAL", "1")
    out = ibkr.cycle(gw, "a1", {**R, "real": True}, send=True, confirm=False, now=OPEN, prices=PRICES)
    assert out["tipo"] == "real" and gw.sent[0][1] is False  # real: no automatic "yes" without --confirmar


def test_cycle_holds_orders_when_stopped_closed_or_signed_out(lab):
    gw = FakeGateway()
    (lab / "PARAR").write_text("")
    out = ibkr.cycle(gw, "a1", R, send=True, confirm=False, now=OPEN, prices=PRICES)
    assert out["ordens"][0]["resultado"] == {"nao_enviada": "PARAR"} and not gw.sent
    (lab / "PARAR").unlink()
    night = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)
    assert ibkr.cycle(gw, "a1", R, send=True, confirm=False, now=night, prices=PRICES)["ordens"][0]["resultado"] == {
        "nao_enviada": "bolsa fechada"}
    assert "sessão" in ibkr.cycle(FakeGateway(authenticated=False), "a1", R, send=True, confirm=False, now=OPEN,
                                  prices=PRICES)["erro"]


def test_cycle_leaves_positions_bought_by_hand_alone(lab):
    gw = FakeGateway(positions=[{"conid": 1, "position": 10}, {"conid": 999, "position": 3, "contractDesc": "MINHA"}])
    out = ibkr.cycle(gw, "a1", R, send=False, confirm=False, now=OPEN, prices=PRICES)
    assert out["fora_do_agente"] == ["MINHA"] and all(o["symbol"] != "MINHA" for o in out["ordens"])


def test_virtual_round_mirrors_the_agent_and_keeps_its_history(tmp_path, monkeypatch):
    from datetime import date
    from types import SimpleNamespace

    monkeypatch.setattr(ibkr, "VIRTUAL", tmp_path)
    monkeypatch.setattr(ibkr.agents, "last_close", lambda mkt, s: {"A.DE": 100.0, "B.DE": 50.0}.get(s))
    monkeypatch.setattr(ibkr.agents, "to_eur", lambda mkt, s, p, on: p)
    monkeypatch.setattr(ibkr.agents, "position_value", lambda mkt, s, pos: pos["qty"] * 100.0)
    monkeypatch.setattr(ibkr.agents, "real_capital", lambda: 5000.0)
    config = {"agents": [{"id": "a1", "congelado": {"ate": "2027-11-05"}}, {"id": "a2"}]}
    state = {"agents": {"a1": {"cash": 500.0, "positions": {"A.DE": {"qty": 5}}, "pending": []},
                        "a2": {"cash": 1000.0, "positions": {}, "pending": []}}}
    mkt = SimpleNamespace(as_of=date(2026, 11, 5))
    out = ibkr.virtual_round(config, state, mkt)
    assert set(out) == {"a1"} and out["a1"]["ordens"] == 1  # only the frozen candidate; 50% in A.DE
    v = ibkr.virtual_summary("a1")
    assert v["posicoes"] == 1 and -0.01 < v["retorno"] < 0
    assert ibkr.virtual_round(config, state, mkt)["a1"]["ordens"] == 0  # already there: nothing more
