import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agent_review  # noqa: E402
import agents  # noqa: E402
import pretrain  # noqa: E402

STRATEGY = "guardiao"


def test_clean_keeps_only_known_params_within_bounds():
    lo, hi, _ = agents.PARAM_BOUNDS["vix_max"]
    out = agent_review.clean(STRATEGY, {"vix_max": hi * 10, "credito_max": 0, "inventado": 3, "stop_atr": True})
    assert out == {"vix_max": hi, "credito_max": 0}


def test_validate_drops_unknown_strategies_and_caps_proposals():
    props = [{"params": {"vix_max": 30 + i}, "hipotese": "h"} for i in range(6)] + [{"params": {"x": 1}}]
    review = agent_review.validate({"geral": "g", "estrategias": {STRATEGY: {"parecer": "p", "propostas": props},
                                                                   "nao_existe": {"parecer": "p"}}})
    assert list(review["estrategias"]) == [STRATEGY]
    assert len(review["estrategias"][STRATEGY]["propostas"]) == agent_review.MAX_PROPOSALS


def test_claude_candidates_build_on_last_choice(tmp_path, monkeypatch):
    review = tmp_path / "review.json"
    report = tmp_path / "pretrain.json"
    review.write_text(json.dumps({"estrategias": {STRATEGY: {"propostas": [
        {"params": {"vix_max": 30}, "hipotese": "menos risco"},
        {"params": {"vix_max": 30}, "hipotese": "repetida"},
        {"params": {"nao_existe": 1}, "hipotese": "ignorada"},
    ]}}}))
    report.write_text(json.dumps({"strategies": {STRATEGY: {"chosen": {"credito_max": 0.1, "por_zona": {"x": {}}}}}}))
    monkeypatch.setattr(pretrain, "REVIEW_PATH", review)
    monkeypatch.setattr(pretrain, "REPORT_PATH", report)
    base = pretrain.variants(STRATEGY, 0)[0]
    out = pretrain.claude_candidates(STRATEGY, base)
    assert len(out) == 1
    assert out[0]["params"]["vix_max"] == 30 and out[0]["params"]["credito_max"] == 0.1
    assert "por_zona" not in out[0]["params"]
    assert out[0]["changes"] == {"vix_max": 30}


def test_claude_candidates_without_review(tmp_path, monkeypatch):
    monkeypatch.setattr(pretrain, "REVIEW_PATH", tmp_path / "nada.json")
    assert pretrain.claude_candidates(STRATEGY, pretrain.variants(STRATEGY, 0)[0]) == []
