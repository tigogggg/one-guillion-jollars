import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import limits  # noqa: E402

NOW = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)


def run(i, conclusion="success", created="2026-11-03T12:00:00Z"):
    return {"id": i, "status": "completed", "conclusion": conclusion, "created_at": created, "html_url": f"https://x/{i}"}


def test_news_section_reads_counts_back_from_annotations(monkeypatch):
    notes = {
        1: [{"message": "40 ações, 120 títulos das últimas 36 h (Yahoo RSS: 90, Google: 30), 25 novos (95 já vistos)"},
            {"message": "25 títulos analisados (Claude), 3 importantes"}],
        2: [{"message": "40 ações, 60 títulos das últimas 36 h (Yahoo RSS: 60), 10 novos (50 já vistos)"},
            {"message": "Claude falhou: rate limit"},
            {"message": "10 títulos analisados (FinBERT), nenhum importante"}],
    }
    monkeypatch.setattr(limits, "runs", lambda wf, since, pages=10: [run(1), run(2, "failure")])
    monkeypatch.setattr(limits, "annotations", lambda r: notes[r["id"]])
    lines, _ = limits.news_section(NOW)
    text = "\n".join(t for _, t in lines)
    assert "Títulos encontrados: 180 (35 novos)" in text
    assert "Yahoo RSS 150" in text and "Google 30" in text
    assert "Claude 25 (71%)" in text and "FinBERT (reserva) 10" in text
    assert "Falhas do Claude: 1" in text
    assert "1 ok, 1 falhadas" in text
    assert sum(not ok for ok, _ in lines) == 3  # a failed run, Claude under 90%, one Claude failure


def test_job_minutes_round_each_job_up(monkeypatch):
    monkeypatch.setattr(limits, "jobs", lambda r: [
        {"started_at": "2026-11-03T12:00:00Z", "completed_at": "2026-11-03T12:00:10Z"},  # 10 s -> 1
        {"started_at": "2026-11-03T12:00:00Z", "completed_at": "2026-11-03T12:02:01Z"},  # 2 min 1 s -> 3
        {"started_at": "2026-11-03T12:00:00Z", "completed_at": None},  # still running
    ])
    assert limits.job_minutes({"id": 1}) == 4


def test_actions_section_projects_the_month(monkeypatch):
    monkeypatch.setattr(limits, "runs", lambda wf, since, pages=10: [run(i) for i in range(50)])
    monkeypatch.setattr(limits, "job_minutes", lambda r: 6)  # 300 minutes in 2.9 days
    lines = limits.actions_section(NOW)
    assert "~300 de 2000 (15%)" in lines[0][1] and lines[0][0]
    assert not lines[1][0]  # ~3100 at this pace: over the free 2000
