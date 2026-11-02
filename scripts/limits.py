"""/limites: how the news reading and the stock analysis are doing, and how much of each limit is used.

Three parts, each from the GitHub API (what the scheduled jobs reported) plus
one small live test per source:

- 📰 News, last 7 days: runs, headlines per source, how many Claude read and
  how many fell back to FinBERT, Claude failures; live: one tiny Claude request
  and one headline search.
- 📊 Stock data: FMP calls of the last tips run (free tier 250/day), live FMP
  and Yahoo Finance answers, and how fresh the two breadth CSVs are.
- ⚙️ GitHub Actions minutes this month (the repo is private: 2000/month free).

    GITHUB_REPOSITORY=owner/repo GITHUB_TOKEN=... uv run python scripts/limits.py
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from discord_post import GREEN, REPO, embed
from health import DATA_SOURCES, ORANGE, data_line, last_data_date

LISBON = ZoneInfo("Europe/Lisbon")
API = "https://api.github.com"
FMP_DAILY = 250  # FMP free tier
ACTIONS_MONTHLY = 2000  # GitHub Free, private repos
CLAUDE_MODEL = "haiku"
NEWS_DAYS = 7

FETCHED = re.compile(r"(\d+) ações, (\d+) títulos das últimas 36 h \((.*?)\), (\d+) novos")
RATED = re.compile(r"(\d+) títulos analisados \((\w+)\)")
SOURCE = re.compile(r"([\w ]+?): (\d+)")
FMP_CALLS = re.compile(r"(\d+) chamadas")


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


def gh(url: str, **params) -> dict | list | None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    url = url if url.startswith("http") else f"{API}/repos/{os.environ.get('GITHUB_REPOSITORY', '')}/{url}"
    try:
        resp = requests.get(url, params=params, timeout=30,
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"WARN: GitHub {url}: {e}", file=sys.stderr)
        return None


def runs(workflow: str | None, since: datetime, pages: int = 10) -> list[dict]:
    """Runs on main created since `since`, newest first (all workflows when workflow is None)."""
    path = f"actions/workflows/{workflow}/runs" if workflow else "actions/runs"
    out = []
    for page in range(1, pages + 1):
        data = gh(path, created=f">={since:%Y-%m-%dT%H:%M:%SZ}", per_page=100, page=page)
        batch = (data or {}).get("workflow_runs", [])
        out += batch
        if len(batch) < 100:
            break
    return out


def jobs(run: dict) -> list[dict]:
    return (gh(f"actions/runs/{run['id']}/jobs", per_page=100) or {}).get("jobs", [])


def annotations(run: dict) -> list[dict]:
    out = []
    for job in jobs(run):
        out += gh(f"{job['check_run_url']}/annotations", per_page=100) or []
    return out


def when(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LISBON).strftime("%d/%m %H:%M")


def run_status(run: dict | None) -> str:
    if run is None:
        return "❔ sem registo"
    if run["status"] != "completed":
        return f"⏳ a correr desde {when(run['created_at'])}"
    ok = run["conclusion"] in ("success", "skipped")
    return f"{'✅' if ok else '❌'} {when(run['created_at'])}" + ("" if ok else f" · [ver]({run['html_url']})")


# --------------------------------------------------------------------------
# Live tests
# --------------------------------------------------------------------------


def claude_test() -> tuple[bool, str]:
    """One tiny request with the subscription token, as the news job makes it."""
    token = re.sub(r"\s+", "", os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
    if not token or not shutil.which("claude"):
        return False, "❔ Claude: não testado (sem token ou sem o Claude Code)"
    start = time.monotonic()
    try:
        proc = subprocess.run(["claude", "-p", "--output-format", "json", "--model", CLAUDE_MODEL, "--max-turns", "1"],
                              input="Responde apenas: ok", capture_output=True, text=True, timeout=120,
                              env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token})
        out = json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError) as e:
        return False, f"❌ Claude: não respondeu ({str(e)[:120]})"
    if proc.returncode != 0 or out.get("is_error"):
        return False, f"❌ Claude: {(out.get('result') or proc.stderr or 'erro')[:160]}"
    return True, f"✅ Claude ({CLAUDE_MODEL}) responde agora ({time.monotonic() - start:.0f} s)"


def headline_test() -> tuple[bool, str]:
    import news  # heavy imports; only here

    news.SOURCE_COUNTS.clear()
    items = news.headlines("NVDA", name="NVIDIA")
    if not items:
        return False, "⚠️ Títulos: nenhuma fonte devolveu notícias da NVDA nas últimas 36 h"
    return True, f"✅ Títulos: {len(items)} da NVDA via {', '.join(news.SOURCE_COUNTS)}"


def fmp_test() -> tuple[bool, str]:
    """One quote call (of the 250 a day)."""
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return False, "❔ FMP: sem chave (FMP_API_KEY)"
    try:
        resp = requests.get("https://financialmodelingprep.com/stable/quote", params={"symbol": "SPY", "apikey": key}, timeout=30)
    except requests.RequestException as e:
        return False, f"❌ FMP: não respondeu ({str(e)[:100]})"
    if resp.status_code == 429:
        return False, f"❌ FMP: limite diário de {FMP_DAILY} chamadas atingido"
    if resp.status_code in (401, 403):
        return False, f"❌ FMP: chave rejeitada ({resp.status_code})"
    if resp.status_code != 200:
        return False, f"❌ FMP: respondeu {resp.status_code}"
    return True, "✅ FMP responde agora"


def yahoo_test() -> tuple[bool, str]:
    import yfinance as yf

    start = time.monotonic()
    try:
        df = yf.download("SPY", period="5d", progress=False, auto_adjust=True)
    except Exception as e:  # yfinance raises many kinds
        return False, f"❌ Yahoo Finance: {str(e)[:120]}"
    if df is None or df.empty:
        return False, "❌ Yahoo Finance: sem dados do SPY"
    return True, f"✅ Yahoo Finance: preços do SPY até {df.index[-1]:%d/%m} ({time.monotonic() - start:.0f} s)"


# --------------------------------------------------------------------------
# Schedule (the Worker's KV, as /horario sets it)
# --------------------------------------------------------------------------

DEFAULT_SCHEDULE = {  # keep in step with DEFAULT_SCHEDULE in discord-bot/worker.js
    "dicas": "07:00", "agentes": "07:30", "ronda": "08:05-20:55/10",
    "alertas": ["15:40", "17:40", "19:40"], "acompanhamento": "22:30",
    "estado": "23:00", "noticias": ["12:00", "16:00", "20:30"], "semanal": {"dia": "sab", "hora": "10:00"},
    "treino": "06:00",
}
DAY_NAMES = {"dom": "domingo", "seg": "segunda", "ter": "terça", "qua": "quarta", "qui": "quinta", "sex": "sexta",
             "sab": "sábado"}


def schedule() -> dict:
    import discord_bot

    saved = dict(discord_bot.kv_get("schedule") or {})
    if saved.get("v", 1) < 2:  # saved before the agents moved to the European open (as getSchedule in worker.js)
        saved.pop("dicas", None)
        saved.pop("agentes", None)
    return {**DEFAULT_SCHEDULE, **saved}


def times_text(times: str | list[str]) -> str:
    times = [times] if isinstance(times, str) else list(times)
    return f"{len(times)}× por dia útil · " + " · ".join(times)


def last_day(found: list[dict]) -> str:
    """The runs of the most recent day with any, oldest first: '30/10: ✅ 12:04 · ❌ 16:03'."""
    if not found:
        return "sem execuções nos últimos 7 dias"
    local = lambda r: datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(LISBON)  # noqa: E731
    day = max(local(r).date() for r in found)
    marks = []
    for r in sorted((r for r in found if local(r).date() == day), key=local):
        mark = "⏳" if r["status"] != "completed" else ("✅" if r["conclusion"] in ("success", "skipped") else "❌")
        marks.append(f"{mark} {local(r):%H:%M}")
    return f"{day:%d/%m}: " + " · ".join(marks)


def latest_note(found: list[dict], title: str, pattern: re.Pattern) -> re.Match | None:
    """The newest run's annotation with this title that matches, looking back a few runs."""
    for r in [r for r in found if r["status"] == "completed"][:3]:
        for a in annotations(r):
            if a.get("title") == title and (m := pattern.search(a.get("message", ""))):
                return m
    return None


RANKINGS_NOTE = re.compile(r"(\d+) de (\d+) ações do S&P 500 comparadas em (\d+) setores; (\d+) de (\d+) ETFs")
ALERTS_NOTE = re.compile(r"(\d+) ações comparadas")
VCP_NOTE = re.compile(r"(\S+) ações analisadas no ecrã VCP, (\d+) chamadas")
NEWS_STOCKS = re.compile(r"(\d+) ações, \d+ títulos")


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def news_section(now: datetime, sched: dict | None = None) -> list[tuple[bool, str]]:
    recent = runs("news.yml", now - timedelta(days=NEWS_DAYS))
    with ThreadPoolExecutor(8) as pool:
        notes = list(pool.map(annotations, recent))
    sources: dict[str, int] = {}
    by_reader = {"Claude": 0, "FinBERT": 0}
    fetched = new = failures = 0
    stocks = None
    for found in notes:  # newest run first
        for a in found:
            msg = a.get("message", "")
            if m := FETCHED.search(msg):
                fetched += int(m.group(2))
                new += int(m.group(4))
                stocks = stocks if stocks is not None else int(m.group(1))
                for name, n in SOURCE.findall(m.group(3)):
                    sources[name.strip()] = sources.get(name.strip(), 0) + int(n)
            elif m := RATED.search(msg):
                by_reader[m.group(2)] = by_reader.get(m.group(2), 0) + int(m.group(1))
            elif msg.startswith("Claude falhou"):
                failures += 1
    done = [r for r in recent if r["status"] == "completed"]
    failed = sum(r["conclusion"] not in ("success", "skipped") for r in done)
    read = sum(by_reader.values())
    lines = []
    if sched:
        lines.append((True, f"🕒 Horário: {times_text(sched['noticias'])}"))
    lines += [
        (True, f"📅 Último dia: {last_day(recent)}"),
        (True, f"🔎 Compara: notícias de {stocks if stocks is not None else '?'} ações (ranking, carteiras dos agentes, "
               "/seguir e as 10 que mais mexeram)"),
        (failed == 0, f"{'✅' if not failed else '❌'} {NEWS_DAYS} dias: {len(done)} leituras, {failed} falhadas · "
                      f"{fetched} títulos ({new} novos)"
                      + (" · " + " · ".join(f"{k} {v}" for k, v in sources.items()) if sources else "")),
    ]
    if read:
        share = by_reader.get("Claude", 0) / read
        lines.append((share >= 0.9, f"{'✅' if share >= 0.9 else '⚠️'} Lidos: {read} · Claude {by_reader.get('Claude', 0)} "
                                    f"({share:.0%}) · FinBERT (reserva) {by_reader.get('FinBERT', 0)}"))
    lines.append((failures == 0, f"{'✅' if not failures else '⚠️'} Falhas do Claude: {failures}"))
    return lines


def tips_section(now: datetime, sched: dict) -> list[tuple[bool, str]]:
    found = runs("discord-daily.yml", now - timedelta(days=NEWS_DAYS), pages=1)
    lines = [(True, f"🕒 Horário: {times_text(sched['dicas'])} (dicas, VCP e rankings)"),
             (True, f"📅 Último dia: {last_day(found)}")]
    if m := latest_note(found, "Rankings", RANKINGS_NOTE):
        stocks, sectors, etfs = f"{m.group(1)} de {m.group(2)}", m.group(3), f"{m.group(4)} de {m.group(5)}"
    else:  # before the runs reported it: the lists themselves
        import rankings

        constituents = rankings.load_constituents()
        stocks, sectors, etfs = str(len(constituents)), str(len({c["sector"] for c in constituents})), str(len(rankings.UCITS_ETFS))
    lines.append((True, f"🔎 Compara: {stocks} ações do S&P 500 em {sectors} setores (top 3 por setor) "
                        f"e {etfs} ETFs UCITS (top 10)"))
    vcp = latest_note(found, "FMP", VCP_NOTE)
    calls = int(vcp.group(2)) if vcp else next(
        (int(m.group(1)) for r in found[:1] for a in annotations(r)
         if a.get("title") == "FMP" and (m := FMP_CALLS.search(a.get("message", "")))), None)
    universe = vcp.group(1) if vcp else "240"
    if calls is not None:
        lines.append((calls < FMP_DAILY, f"{'✅' if calls < FMP_DAILY else '⚠️'} VCP: {universe} ações com o FMP, "
                                         f"{calls} de {FMP_DAILY} chamadas/dia ({calls / FMP_DAILY:.0%})"))
    else:
        lines.append((True, f"🔍 VCP: {universe} ações com o FMP (limite {FMP_DAILY} chamadas/dia)"))
    return lines


def alerts_section(now: datetime, sched: dict) -> list[tuple[bool, str]]:
    found = runs("opportunity-alerts.yml", now - timedelta(days=NEWS_DAYS), pages=1)
    m = latest_note(found, "Alertas", ALERTS_NOTE)
    watched = f"{m.group(1)} ações" if m else "as ações da watchlist (top 3 por setor + setups VCP)"
    return [
        (True, f"🕒 Horário: {times_text(sched['alertas'])} · acompanhamento {sched['acompanhamento']}"),
        (True, f"📅 Último dia: {last_day(found)}"),
        (True, f"🔎 Compara: {watched} com o pivot e o volume do dia"),
    ]


def agents_section(now: datetime, sched: dict) -> list[tuple[bool, str]]:
    found = [r for r in runs("agents.yml", now - timedelta(days=NEWS_DAYS), pages=1)
             if "config" not in r.get("display_title", "")]  # /agentes, /estrategia… use the same workflow
    try:
        active = sum(1 for a in json.loads((REPO / "agents" / "config.json").read_text())["agents"] if a.get("active", True))
    except (OSError, ValueError, KeyError):
        active = "?"
    weekly = sched["semanal"]
    return [
        (True, f"🕒 Horário: {times_text(sched['agentes'])} · {active} agentes"),
        (True, f"📅 Último dia: {last_day(found)}"),
        (True, f"🗓️ Resumo semanal: {DAY_NAMES.get(weekly['dia'], weekly['dia'])} {weekly['hora']} · "
               f"re-treino: 1.º dia útil do mês {sched['treino']} · estado do bot: {sched['estado']}"),
    ]


def sources_section(now: datetime) -> list[tuple[bool, str]]:
    today = now.astimezone(LISBON).date()
    return [claude_test(), headline_test(), fmp_test(), yahoo_test()] + [
        data_line(name, last_data_date(url, col), today) for name, url, col in DATA_SOURCES]


def job_minutes(run: dict) -> int:
    """Billable minutes of one run: each job rounded up to the minute."""
    total = 0
    for job in jobs(run):
        if job.get("started_at") and job.get("completed_at"):
            start = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
            total += max(1, math.ceil((end - start).total_seconds() / 60)) if end > start else 0
    return total


def actions_section(now: datetime) -> list[tuple[bool, str]]:
    month = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_runs = [r for r in runs(None, month, pages=20) if r["status"] == "completed"]
    if not month_runs:
        return [(False, "❔ Sem acesso às execuções do GitHub")]
    with ThreadPoolExecutor(10) as pool:
        minutes = sum(pool.map(job_minutes, month_runs))
    days = (now.astimezone(timezone.utc) - month).total_seconds() / 86400
    next_month = (month + timedelta(days=32)).replace(day=1)
    pace = minutes / max(days, 1) * (next_month - month).days
    share = minutes / ACTIONS_MONTHLY
    ok = pace <= ACTIONS_MONTHLY
    return [
        (share < 0.8, f"{'✅' if share < 0.8 else '⚠️'} Minutos este mês: ~{minutes} de {ACTIONS_MONTHLY} ({share:.0%}), "
                      f"{len(month_runs)} execuções"),
        (ok, f"{'✅' if ok else '⚠️'} Ao ritmo atual: ~{pace:.0f} no fim do mês"),
    ]


def build_card(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sched = schedule()
    sections = [
        ("📰 Leitura de notícias", news_section(now, sched)),
        ("📊 Dicas do dia e rankings", tips_section(now, sched)),
        ("🚨 Alertas de rompimento", alerts_section(now, sched)),
        ("🤖 Liga dos agentes e outras tarefas", agents_section(now, sched)),
        ("🔌 Fontes agora", sources_section(now)),
        ("⚙️ GitHub Actions (repo privado)", actions_section(now)),
    ]
    problems = sum(not ok for _, lines in sections for ok, _ in lines)
    return embed(
        "🔋 Rotina, limites e fontes" + (": tudo bem" if not problems else f": {problems} a ver"),
        "Quantas vezes e a que horas cada tarefa corre (hora de Lisboa), o que compara, e se as fontes respondem.",
        color=GREEN if not problems else ORANGE,
        fields=[(name, "\n".join(t for _, t in lines)) for name, lines in sections],
        footer="Horários do /horario; \"Último dia\" mostra as execuções reais. O Claude Max não mostra a quota que "
               "resta: o teste diz se responde agora. Minutos estimados por tarefa, arredondados ao minuto.",
    )


def main() -> int:
    print(json.dumps(build_card(), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
