import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import portfolio_analysis  # noqa: E402
import pretrain  # noqa: E402


def test_claude_answer_is_parsed_and_stored(monkeypatch):
    stored = {}
    answer = {"resumo": "Carteira concentrada.", "nota": 6, "movimentos": [{"acao": "reforçar", "ativo": "EUNL.DE"}]}
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x\n")
    monkeypatch.setattr(portfolio_analysis.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(portfolio_analysis.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stderr="", stdout=json.dumps({"result": "Aqui vai:\n" + json.dumps(answer, ensure_ascii=False)})))
    monkeypatch.setattr(portfolio_analysis.discord_bot, "kv_get", lambda key: {"portfolio": {"holdings": []}} if key == "analysis-req:" + "a" * 24 else None)
    monkeypatch.setattr(portfolio_analysis.discord_bot, "kv_put", lambda key, value, ttl_days=None: stored.update({key: value}) or True)
    monkeypatch.setattr(sys, "argv", ["x", "a" * 24])
    assert portfolio_analysis.main() == 0
    res = stored["analysis-res:" + "a" * 24]
    assert res["status"] == "done" and res["result"]["nota"] == 6 and res["result"]["movimentos"][0]["ativo"] == "EUNL.DE"


def test_a_bad_answer_is_stored_as_an_error(monkeypatch):
    stored = {}
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(portfolio_analysis.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(portfolio_analysis.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"result": "não sei"})))
    monkeypatch.setattr(portfolio_analysis.discord_bot, "kv_get", lambda key: {"x": 1})
    monkeypatch.setattr(portfolio_analysis.discord_bot, "kv_put", lambda key, value, ttl_days=None: stored.update({key: value}) or True)
    monkeypatch.setattr(sys, "argv", ["x", "b" * 24])
    assert portfolio_analysis.main() == 1
    assert stored["analysis-res:" + "b" * 24]["status"] == "error"


def test_light_curve_keeps_the_last_year_daily():
    curve = [(f"d{i}", float(i)) for i in range(2000)]
    light = pretrain.light_curve(curve)
    assert light[-pretrain.DAILY_TAIL:] == curve[-pretrain.DAILY_TAIL:]
    assert len(light) == (2000 - pretrain.DAILY_TAIL + 4) // 5 + pretrain.DAILY_TAIL
