from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_parses(path):
    """GitHub silently skips a workflow it cannot read (a ':' in a step name broke two in November 2026)."""
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict) and data.get("jobs"), path.name
