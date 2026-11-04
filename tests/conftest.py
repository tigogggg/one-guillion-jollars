import os

# The tests use the default broker's costs, whatever the local agents/config.json says (agents reads it on import).
os.environ["OGJ_CORRETORA"] = "xtb"


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_claude_usage_records(monkeypatch, tmp_path_factory):
    """Tests that fake a Claude reply must not leave usage records in the real agents/ checkout."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import claude_usage

    monkeypatch.setattr(claude_usage, "USAGE_DIR", tmp_path_factory.mktemp("uso_claude"))
