import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402
import claude_usage  # noqa: E402


def test_each_call_leaves_a_record_and_the_month_adds_up(monkeypatch, tmp_path):
    monkeypatch.setattr(agents, "REPO_AGENTS", tmp_path)
    monkeypatch.setattr(claude_usage, "USAGE_DIR", tmp_path / "lab" / "uso_claude")
    out = {"usage": {"input_tokens": 100, "cache_read_input_tokens": 5000, "output_tokens": 300}, "num_turns": 3,
           "duration_ms": 9000, "total_cost_usd": 0.12, "is_error": False}
    for s in range(3):
        claude_usage.record("visao", "sonnet", out, datetime(2026, 11, 4, 12, 0, s, tzinfo=UTC))
    claude_usage.record("noticias", "haiku", {"usage": {"input_tokens": 50}, "total_cost_usd": 0.01},
                        datetime(2026, 11, 5, tzinfo=UTC))
    claude_usage.record("x", "haiku", None)  # nothing to record, no error
    files = list((tmp_path / "lab" / "uso_claude" / "2026-11").glob("*.json"))
    assert len(files) == 4 and json.loads(files[0].read_text())["model"] in ("sonnet", "haiku")
    month = claude_usage.summary()["2026-11"]
    assert month["visao"]["chamadas"] == 3 and month["visao"]["cache_read_input_tokens"] == 15000
    assert month["visao"]["custo_usd"] == 0.36 and month["noticias"]["chamadas"] == 1
