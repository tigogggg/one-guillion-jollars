"""Daily health check of the bot, for the 🔧-estado channel.

- Did today's scheduled jobs run on main, and did they succeed? (GitHub API)
- Is the breadth data fresh? (the two public CSVs the daily tips depend on)

Posts one short card to the "estado" channel of discord/channels.json and
never anywhere else: until that channel exists the check only prints. Started
by the Worker on weekdays after every other job (`/horario estado`).

    uv run python scripts/health.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from discord_post import GREEN, embed, route, send

LISBON = ZoneInfo("Europe/Lisbon")
# Weekday jobs the Worker starts: (workflow, name, minimum runs per day, run-name prefix or None).
WEEKDAY_JOBS = [
    ("discord-daily.yml", "Dicas do dia e rankings", 1, None),
    ("agents.yml", "Liga dos agentes", 1, "Agentes: daily"),
    ("agents.yml", "Rondas dos agentes", 20, "Agentes: stops"),
    ("opportunity-alerts.yml", "Alertas", 1, None),
    ("news.yml", "Notícias", 1, None),
    ("alert-followups.yml", "Acompanhamento dos alertas", 1, None),
]
# Every day (the Worker starts them too since GitHub's own cron proved unreliable), and the view on Mon/Thu.
DAILY_JOBS = [("backup.yml", "Cópia de segurança", 1, None), ("tests.yml", "Testes", 1, None)]
VISION_JOB = ("visao.yml", "Visão a 18 meses", 1, None)


def named(runs: list[dict] | None, prefix: str | None) -> list[dict] | None:
    """Only the runs whose run-name starts with `prefix` (agents.yml runs the daily league and the rounds)."""
    if runs is None or prefix is None:
        return runs
    return [r for r in runs if str(r.get("display_title") or r.get("name") or "").startswith(prefix)]
DATA_SOURCES = [
    ("Breadth (uptrend)", "https://raw.githubusercontent.com/tradermonty/uptrend-dashboard/main/data/uptrend_ratio_timeseries.csv", "date"),
    ("Breadth (S&P 500)", "https://tradermonty.github.io/market-breadth-analysis/market_breadth_data.csv", "Date"),
]
MAX_DATA_LAG = 2  # weekdays: Friday's data is still fresh on Monday evening
ORANGE = 0xF0B232


def runs_today(workflow: str, today: date) -> list[dict] | None:
    """Today's (Lisbon) runs of a workflow (main, or estavel where the agents run), or None when the API is unreachable."""
    runs = runs_since(workflow, today - timedelta(days=1), pages=3)
    return None if runs is None else [
        r for r in runs if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(LISBON).date() == today]


def runs_since(workflow: str, since: date, pages: int = 3) -> list[dict] | None:
    """A workflow's runs created since that day, or None when the API is unreachable."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return None
    runs = []
    try:
        for page in range(1, pages + 1):  # agents.yml runs ~80 times a weekday (league and rounds)
            resp = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs",
                                params={"created": f">={since}", "per_page": 100,
                                        "page": page},
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                                timeout=30)
            resp.raise_for_status()
            batch = resp.json()["workflow_runs"]
            runs += batch
            if len(batch) < 100:
                break
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"WARN: GitHub {workflow}: {e}", file=sys.stderr)
        return None
    return runs


WEEKLY_WORKFLOWS = ["agents.yml", "news.yml", "discord-daily.yml", "opportunity-alerts.yml", "pretrain.yml", "juri.yml",
                    "visao.yml", "backup.yml", "tests.yml", "context-data.yml"]


def weekly_card(today: date) -> dict:
    """Fridays: the week of the system in one card (failures, how long a round takes, Claude's use, the private data's
    size), to see a trend before it becomes a problem."""
    since = today - timedelta(days=7)
    lines, failures = [], []
    for wf in WEEKLY_WORKFLOWS:
        runs = runs_since(wf, since, pages=6 if wf == "agents.yml" else 1)
        if runs is None:
            lines.append(f"❔ {wf}: sem acesso ao GitHub")
            continue
        done = [r for r in runs if r["status"] == "completed"]
        bad = [r for r in done if r["conclusion"] not in ("success", "skipped", "cancelled")]
        if bad:
            failures.append(f"{wf}: {len(bad)} de {len(done)}")
        if wf == "agents.yml":
            secs = sorted((datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00"))
                           - datetime.fromisoformat(r["run_started_at"].replace("Z", "+00:00"))).total_seconds()
                          for r in done if str(r.get("display_title", "")).startswith("Agentes: stops") and r.get("run_started_at"))
            if secs:
                lines.append(f"⏱️ Rondas: {len(secs)} · duração mediana {secs[len(secs) // 2] / 60:.1f} min "
                             f"(a mais lenta {secs[-1] / 60:.1f} min)")
    fields = [("Corridas da semana", "\n".join(lines) or "—"),
              ("Falhas", "\n".join(f"❌ {f}" for f in failures) or "✅ nenhuma")]
    try:
        import claude_usage

        month = claude_usage.summary(1)
        for m, rows in month.items():
            calls = sum(int(r["chamadas"]) for r in rows.values())
            tokens = sum(r[k] for r in rows.values() for k in claude_usage.TOKENS)
            usd = sum(r["custo_usd"] for r in rows.values())
            top = max(rows.items(), key=lambda kv: kv[1]["custo_usd"])[0] if rows else "—"
            fields.append(("🤖 Claude este mês", f"{calls} chamadas · {tokens / 1e6:.1f} M tokens · ~{usd:.2f} USD "
                                                 f"equivalentes na API · quem mais gasta: {top}"))
    except Exception as e:  # noqa: BLE001 - without the private checkout there is no usage to show
        print(f"WARN: uso do Claude: {e!r}", file=sys.stderr)
    agents_dir = Path(__file__).resolve().parents[1] / "agents"
    if agents_dir.is_dir():
        size = sum(f.stat().st_size for f in agents_dir.rglob("*") if f.is_file() and ".git" not in f.parts)
        fields.append(("💾 Dados privados", f"{size / 1e6:.0f} MB nos ficheiros (o histórico do git comprime-os)"))
    return embed(f"🗓️ Semana do sistema · até {today:%d/%m}", "", color=GREEN if not failures else ORANGE, fields=fields,
                 footer="Resumo automático às sextas-feiras.")


def job_line(name: str, runs: list[dict] | None, minimum: int) -> tuple[bool, str]:
    if runs is None:
        return False, f"❔ {name}: não consegui ver no GitHub"
    done = [r for r in runs if r["status"] == "completed"]
    failed = [r for r in done if r["conclusion"] not in ("success", "skipped")]
    if failed:
        return False, f"❌ {name}: {len(failed)} de {len(done)} falharam · [ver]({failed[-1]['html_url']})"
    if len(runs) < minimum:
        return False, f"⚠️ {name}: não correu hoje"
    return True, f"✅ {name}: {len(done)}× ok" + (f" ({len(runs) - len(done)} a correr)" if len(runs) > len(done) else "")


def weekdays_between(start: date, end: date) -> int:
    """Weekdays after `start` up to and including `end`."""
    return sum((start + timedelta(days=n)).weekday() < 5 for n in range(1, (end - start).days + 1))


def last_data_date(url: str, column: str) -> date | None:
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        dates = [r[column][:10] for r in csv.DictReader(io.StringIO(resp.text)) if r.get(column)]
        return date.fromisoformat(max(dates)) if dates else None
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"WARN: dados {url}: {e}", file=sys.stderr)
        return None


def data_line(name: str, last: date | None, today: date) -> tuple[bool, str]:
    if last is None:
        return False, f"❌ {name}: não consegui ler os dados"
    lag = weekdays_between(last, today)
    if lag > MAX_DATA_LAG:
        return False, f"⚠️ {name}: dados de {last:%d/%m}, {lag} dias úteis de atraso"
    return True, f"✅ {name}: dados de {last:%d/%m}"


def build_card(today: date, job_lines: list[tuple[bool, str]], data_lines: list[tuple[bool, str]]) -> dict:
    lines = job_lines + data_lines
    problems = sum(not ok for ok, _ in lines)
    title = f"🔧 Estado do bot · {today:%d/%m}: " + ("tudo a funcionar" if not problems else f"{problems} problema(s)")
    fields = [("Tarefas de hoje", "\n".join(t for _, t in job_lines))] if job_lines else []
    fields.append(("Dados", "\n".join(t for _, t in data_lines)))
    return embed(title, "" if job_lines else "Fim de semana: só os dados são verificados.",
                 color=GREEN if not problems else ORANGE, fields=fields,
                 footer="Verificação automática no fim do dia. Um ❌ não apaga publicações; mostra o que falhou.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print the card instead of posting")
    args = parser.parse_args()

    today = datetime.now(LISBON).date()
    expected = (WEEKDAY_JOBS if today.weekday() < 5 else []) + DAILY_JOBS + ([VISION_JOB] if today.weekday() in (0, 3) else [])
    jobs = [job_line(name, named(runs_today(wf, today), prefix), n) for wf, name, n, prefix in expected]
    data = [data_line(name, last_data_date(url, col), today) for name, url, col in DATA_SOURCES]
    card = build_card(today, jobs, data)
    cards = [card] + ([weekly_card(today)] if today.weekday() == 4 else [])
    where = route("estado")
    if args.dry_run or where.get("kind") not in ("estado", "testes"):  # never fall back to the webhook's channel
        if not args.dry_run:
            print("Sem canal 🔧-estado em discord/channels.json (stand-by): só mostro o resultado.", file=sys.stderr)
        return send(cards, dry_run=True)
    return send(cards, **where)


if __name__ == "__main__":
    sys.exit(main())
