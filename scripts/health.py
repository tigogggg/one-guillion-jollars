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
from zoneinfo import ZoneInfo

import requests

from discord_post import GREEN, embed, route, send

LISBON = ZoneInfo("Europe/Lisbon")
# Weekday jobs the Worker starts: (workflow, name, minimum runs per day).
WEEKDAY_JOBS = [
    ("discord-daily.yml", "Dicas do dia e rankings", 1),
    ("agents.yml", "Liga dos agentes", 1),
    ("opportunity-alerts.yml", "Alertas", 1),
    ("news.yml", "Notícias", 1),
    ("alert-followups.yml", "Acompanhamento dos alertas", 1),
]
DATA_SOURCES = [
    ("Breadth (uptrend)", "https://raw.githubusercontent.com/tradermonty/uptrend-dashboard/main/data/uptrend_ratio_timeseries.csv", "date"),
    ("Breadth (S&P 500)", "https://tradermonty.github.io/market-breadth-analysis/market_breadth_data.csv", "Date"),
]
MAX_DATA_LAG = 2  # weekdays: Friday's data is still fresh on Monday evening
ORANGE = 0xF0B232


def runs_today(workflow: str, today: date) -> list[dict] | None:
    """Today's (Lisbon) runs of a workflow on main, or None when the API is unreachable."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return None
    try:
        resp = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs",
                            params={"branch": "main", "created": f">={today - timedelta(days=1)}", "per_page": 50},
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                            timeout=30)
        resp.raise_for_status()
        runs = resp.json()["workflow_runs"]
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"WARN: GitHub {workflow}: {e}", file=sys.stderr)
        return None
    return [r for r in runs
            if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(LISBON).date() == today]


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
    jobs = [job_line(name, runs_today(wf, today), n) for wf, name, n in WEEKDAY_JOBS] if today.weekday() < 5 else []
    data = [data_line(name, last_data_date(url, col), today) for name, url, col in DATA_SOURCES]
    card = build_card(today, jobs, data)
    where = route("estado")
    if args.dry_run or where.get("kind") not in ("estado", "testes"):  # never fall back to the webhook's channel
        if not args.dry_run:
            print("Sem canal 🔧-estado em discord/channels.json (stand-by): só mostro o resultado.", file=sys.stderr)
        return send([card], dry_run=True)
    return send([card], **where)


if __name__ == "__main__":
    sys.exit(main())
