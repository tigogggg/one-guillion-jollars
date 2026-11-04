"""The Visionário's view: where the market is going over the next 18 months, written by Claude each week.

After the scenario-analyzer of tradermonty/claude-trading-skills (MIT): Claude reads the last two weeks of news
(geopolitics, wars, elections, government budgets and industrial policy, central banks, technology) with web search,
together with our own numbers (the macro regime, the political agenda by sector, the industries' momentum), and builds
three scenarios (base, bull, bear, with probabilities), traces their first-, second- and third-order effects on sectors
and industries, and ends with tilts: -2 (avoid) to +2 (favour) per GICS sector and per industry (context.INDUSTRY_ETFS).
A second pass (the reviewer) checks the probabilities, the biases and what was missed, as the skill does.

There is no free archive of past news, so this cannot be backtested: the Visionário is judged live, against the
Indexador, like the news agents.

It also gives tilts at 3, 6, 12 and 18 months per sector and per style factor; `--avaliar` scores every past view's
calls against what the market did over each horizon (lab/visao_avaliacao.json), so the horizon and the kind of call
Claude gets right are measured before an agent follows them.

Saved in the private repo: agents/visao.json (the latest, read by agents.s_visionario) and lab/visao_historico.json.

    uv run python scripts/vision.py [--contexto ctx/context.csv] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import claude_usage
import agents
import context

VISION_PATH = agents.REPO_AGENTS / "visao.json"
HISTORY_PATH = agents.REPO_AGENTS / "lab" / "visao_historico.json"
MODEL = "sonnet"
SECTORS = list(context.SECTOR_ETFS)
THEMES = {f"ind_{e}": subs for e, subs in context.INDUSTRY_ETFS.items()}
# Besides the 18-month tilts the Visionário follows, the view gives tilts at 3, 6, 12 and 18 months per sector and per
# style factor (plain, unleveraged ETFs); each one is scored later against what the market did (evaluate), so in time
# we know which horizon and which kind of call Claude gets right, before any agent follows them
HORIZONS = {"3m": 91, "6m": 182, "12m": 365, "18m": 548}  # calendar days
FACTORS = {"valor": "VLUE", "crescimento": "IWF", "qualidade": "QUAL", "momento": "MTUM", "pequenas_empresas": "IWM",
           "baixa_volatilidade": "USMV", "dividendos": "SCHD"}
EVAL_PATH = agents.REPO_AGENTS / "lab" / "visao_avaliacao.json"
HISTORY_KEEP = 500  # views kept (about four years at two a week, so every 18-month call can be scored)
# Headlines of the last two weeks (Google News RSS, as scripts/news.py), handed to Claude with its own web search:
# the search alone found little recent material on the first run
QUERIES = [
    "war OR invasion OR missile OR ceasefire when:14d",
    "election OR Congress OR \"White House\" budget OR \"government shutdown\" when:14d",
    "\"defense spending\" OR \"military spending\" OR NATO OR rearmament when:14d",
    "tariffs OR \"trade deal\" OR \"export controls\" OR sanctions when:14d",
    "\"Federal Reserve\" OR ECB OR \"interest rates\" OR inflation when:14d",
    "\"AI investment\" OR \"data centers\" OR capex OR semiconductors when:14d",
    "infrastructure OR \"energy policy\" OR \"industrial policy\" OR subsidies when:14d",
    "China economy OR Europe economy OR \"stock market\" outlook when:14d",
]


def headlines(limit: int = 120) -> list[dict]:
    import news

    seen, out = set(), []
    for q in QUERIES:
        try:
            items = news.rss_items(news.fetch_rss(news.GOOGLE_RSS, {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}), "MACRO", "Google")
        except Exception as e:  # noqa: BLE001 - one query failing leaves the others
            print(f"::warning::Visão: manchetes ({e!r})", flush=True)
            continue
        for i in items:
            key = i["title"].lower()[:80]
            if key not in seen:
                seen.add(key)
                out.append({"data": i["published"][:10], "titulo": i["title"], "fonte": i["publisher"]})
    return sorted(out, key=lambda i: i["data"], reverse=True)[:limit]

PROMPT = agents.recipe_prompt("visao")  # private: agents/receita/prompts/visao.md


def our_numbers(ctx: dict) -> dict:
    """What our own data says today: the macro regime, the political agenda by sector, the industries' momentum."""
    sectors = {}
    for sec in SECTORS:
        k = context.sector_key(sec)
        sectors[sec] = {f: ctx.get(f"{k}_{f}") for f in ("trend", "rs", "gov", "epub", "capex") if ctx.get(f"{k}_{f}") is not None}
    themes = {k: {"subindustrias": subs[:3], "momento": ctx.get(f"{k}_mom"), "tendencia": ctx.get(f"{k}_trend")} for k, subs in THEMES.items()}
    regime = {k: ctx.get(k) for k in ("reg_conc", "reg_size", "reg_credit", "reg_cycle", "reg_corr", "reg_curve")}
    return {"regime": regime, "setores": sectors, "industrias": themes,
            "notas": "reg_*: variação a 6 meses de RSP/SPY (concentração), IWM/SPY, HYG/LQD, XLY/XLP; reg_corr: correlação "
                     "ações-obrigações; gov: surpresa do gasto público; epub: pouca exposição à incerteza política; "
                     "capex: crescimento do investimento (alto costuma render menos); momento: força face ao S&P 500."}


def ask(numbers: dict) -> dict | None:
    token = re.sub(r"\s+", "", os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
    if not token or not shutil.which("claude") or not PROMPT:
        print("Visão: Claude indisponível", flush=True)
        return None
    try:
        proc = subprocess.run(["claude", "-p", "--output-format", "json", "--model", MODEL, "--max-turns", "25",
                               "--allowedTools", "WebSearch,WebFetch"],
                              input=PROMPT + json.dumps(numbers, ensure_ascii=False, default=str), capture_output=True,
                              text=True, timeout=1500, env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token})
        out = json.loads(proc.stdout or "{}")
        claude_usage.record("visao", MODEL, out)
        if proc.returncode != 0 or out.get("is_error"):
            print(f"::warning::Visão: o Claude falhou ({(out.get('result') or proc.stderr or '')[:200]})", flush=True)
            return None
        match = re.search(r"\{.*\}", out.get("result", ""), re.DOTALL)
        return json.loads(match.group(0)) if match else None
    except (subprocess.TimeoutExpired, ValueError) as e:
        print(f"::warning::Visão: resposta inválida ({e})", flush=True)
        return None


def clean(raw: dict) -> dict | None:
    """Only known sectors and industries, tilts as integers in [-2, 2]; probabilities that add up."""
    def tilt(x) -> int:
        try:
            return max(-2, min(2, int(round(float(x)))))
        except (TypeError, ValueError):
            return 0

    sectors = {s: tilt(v) for s, v in (raw.get("setores") or {}).items() if s in SECTORS}
    themes = {k: tilt(v) for k, v in (raw.get("industrias") or {}).items() if k in THEMES}
    factors = {k: tilt(v) for k, v in (raw.get("fatores") or {}).items() if k in FACTORS}
    horizons = {}
    for h, d in (raw.get("horizontes") or {}).items():
        if h in HORIZONS and isinstance(d, dict):
            hs = {s: tilt(v) for s, v in (d.get("setores") or {}).items() if s in SECTORS}
            hf = {k: tilt(v) for k, v in (d.get("fatores") or {}).items() if k in FACTORS}
            if hs or hf:
                horizons[h] = {"setores": hs, "fatores": hf}
    scenarios = [s for s in (raw.get("cenarios") or []) if isinstance(s, dict)][:3]
    total = sum(float(s.get("probabilidade") or 0) for s in scenarios)
    if not scenarios or not (90 <= total <= 110) or not (sectors or themes):
        return None
    return {"resumo": str(raw.get("resumo", ""))[:1200], "cenarios": [
                {"nome": str(s.get("nome", ""))[:30], "probabilidade": round(float(s.get("probabilidade") or 0)),
                 "descricao": str(s.get("descricao", ""))[:600], "efeitos": [str(e)[:200] for e in (s.get("efeitos") or [])][:6]}
                for s in scenarios],
            "revisao": str(raw.get("revisao", ""))[:600], "setores": sectors, "industrias": themes,
            "fatores": factors, "horizontes": horizons,
            "riscos": [str(r)[:200] for r in (raw.get("riscos") or [])][:6],
            "fontes": [str(u)[:300] for u in (raw.get("fontes") or []) if str(u).startswith("http")][:12]}


def card(v: dict) -> dict:
    from discord_post import BLUE, embed

    pos = lambda d: ", ".join(f"{k.replace('ind_', '')} {x:+d}" for k, x in sorted(d.items(), key=lambda kv: -kv[1]) if x > 0)  # noqa: E731
    neg = lambda d: ", ".join(f"{k.replace('ind_', '')} {x:+d}" for k, x in sorted(d.items(), key=lambda kv: kv[1]) if x < 0)  # noqa: E731
    fields = [(f"{s['nome'].title()} ({s['probabilidade']}%)", s["descricao"][:1024]) for s in v["cenarios"]]
    fields += [("🟢 A favorecer", (pos(v["setores"]) + " · " + pos(v["industrias"])).strip(" ·")[:1024] or "—"),
               ("🔴 A evitar", (neg(v["setores"]) + " · " + neg(v["industrias"])).strip(" ·")[:1024] or "—")]
    if v.get("fatores"):
        fields.append(("🧭 Fatores (18 meses)", ", ".join(f"{k.replace('_', ' ')} {x:+d}" for k, x in
                                                        sorted(v["fatores"].items(), key=lambda kv: -kv[1]) if x)[:1024] or "—"))
    if v.get("horizontes"):
        def line(h: str) -> str:
            d = v["horizontes"][h]
            tilts = {**d.get("setores", {}), **{k.replace("_", " "): x for k, x in d.get("fatores", {}).items()}}
            best = [f"{k} {x:+d}" for k, x in sorted(tilts.items(), key=lambda kv: -kv[1]) if x > 0][:3]
            worst = [f"{k} {x:+d}" for k, x in sorted(tilts.items(), key=lambda kv: kv[1]) if x < 0][:3]
            return f"**{h}**: " + " · ".join(p for p in (", ".join(best), ", ".join(worst)) if p)
        fields.append(("⏱️ Por horizonte", "\n".join(line(h) for h in HORIZONS if h in v["horizontes"])[:1024]))
    if v["riscos"]:
        fields.append(("⚠️ Riscos a vigiar", "\n".join(f"• {r}" for r in v["riscos"])[:1024]))
    title = "🔭 Visão a 18 meses (Visionário)" + (" · extraordinária" if v.get("extraordinaria") else "")
    lead = (f"⚠️ Refeita fora do calendário: {v['extraordinaria']}\n\n" if v.get("extraordinaria") else "") + v["resumo"]
    return embed(title, lead[:4000], color=BLUE, fields=fields)


def _tilts(v: dict) -> dict[str, dict[str, dict[str, int]]]:
    """A view's tilts per horizon: {"3m": {"setores": {...}, "fatores": {...}}, ...}; older views only had 18 months."""
    out = {h: {"setores": d.get("setores") or {}, "fatores": d.get("fatores") or {}} for h, d in (v.get("horizontes") or {}).items()}
    if "18m" not in out and (v.get("setores") or v.get("fatores")):
        out["18m"] = {"setores": v.get("setores") or {}, "fatores": v.get("fatores") or {}}
    return out


def score(tilts: dict[str, int], rel: dict[str, float]) -> dict | None:
    """How a set of tilts did: the return of a long-short book weighted by them (relative to the S&P 500), and how
    often the sign was right."""
    calls = {k: t for k, t in tilts.items() if t and k in rel}
    if not calls:
        return None
    return {"excesso": round(sum(t * rel[k] for k, t in calls.items()) / sum(abs(t) for t in calls.values()), 4),
            "acertos": round(sum((t > 0) == (rel[k] > 0) for k, t in calls.items()) / len(calls), 3),
            "n": len(calls)}


def evaluate(history: list[dict], closes: dict, today) -> dict:
    """Every past view's calls against what happened: per horizon and kind (sectors, factors), the matured ones (the
    whole horizon has gone by) apart from the ones still running (scored up to today, for an early look only)."""
    import pandas as pd

    sym = {**{s: e[0] for s, e in context.SECTOR_ETFS.items()}, **FACTORS}
    spy = closes.get(agents.BENCHMARK)

    def ret(series, start, end) -> float | None:
        if series is None:
            return None
        s = series[(series.index.date >= start) & (series.index.date <= end)]
        return float(s.iloc[-1] / s.iloc[0] - 1) if len(s) >= 2 else None

    rows = []
    for v in history:
        try:
            start = datetime.fromisoformat(v["at"]).date()
        except (KeyError, ValueError):
            continue
        for h, d in _tilts(v).items():
            end = start + timedelta(days=HORIZONS[h])
            last = min(end, today)
            if (last - start).days < 14 or (base := ret(spy, start, last)) is None:
                continue
            for kind in ("setores", "fatores"):
                rel = {k: r - base for k in d[kind] if (r := ret(closes.get(sym.get(k)), start, last)) is not None}
                if (sc := score(d[kind], rel)) is not None:
                    rows.append({"visao": v["at"], "horizonte": h, "tipo": kind, "maduro": end <= today, **sc})
    summary: dict = {}
    if rows:
        df = pd.DataFrame(rows)
        for (h, kind, ripe), g in df.groupby(["horizonte", "tipo", "maduro"]):
            summary.setdefault(h, {}).setdefault(kind, {})["maduras" if ripe else "em_curso"] = {
                "visoes": len(g), "excesso_medio": round(float(g["excesso"].mean()), 4),
                "acertos": round(float(g["acertos"].mean()), 3), "visoes_positivas": int((g["excesso"] > 0).sum())}
    return {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "resumo": summary, "avaliacoes": rows[-2000:]}


def run_evaluation() -> None:
    from rankings import download_history

    try:
        hist = json.loads(HISTORY_PATH.read_text())
    except (OSError, ValueError):
        return
    if not hist:
        return
    syms = [agents.BENCHMARK, *(e[0] for e in context.SECTOR_ETFS.values()), *FACTORS.values()]
    data = download_history(syms, period="5y")
    closes = {s: df["Close"].dropna() for s, df in data.items() if df is not None and "Close" in df}
    result = evaluate(hist, closes, datetime.now(timezone.utc).date())
    EVAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVAL_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    print(f"Visão: {len(result['avaliacoes'])} avaliações guardadas no repositório privado", flush=True)


GEO_SHOCK = 4  # the news module's geopolitical risk (0-5) from which a view is redone at once
VIX_SHOCK, VIX_JUMP = 30.0, 0.30  # or the VIX at least this high and up this much in five sessions
EXTRA_AFTER = timedelta(hours=24)  # never more often than this


def shock(now: datetime) -> str | None:
    """Why a view should be redone now (a grave event between the scheduled ones), or None."""
    try:
        v = json.loads(VISION_PATH.read_text())
        if now - datetime.fromisoformat(v["at"]) < EXTRA_AFTER:
            return None
    except (OSError, ValueError, KeyError):
        pass
    try:
        import news

        geo = json.loads(news.GEO_PATH.read_text())
        if (geo.get("risk") or 0) >= GEO_SHOCK and now - datetime.fromisoformat(geo["at"]) < timedelta(hours=12):
            return f"risco geopolítico {geo['risk']}/5: {str(geo.get('summary', ''))[:200]}"
    except (OSError, ValueError, KeyError):
        pass
    try:
        from rankings import download_history

        vix = download_history(["^VIX"], period="1mo").get("^VIX")
        if vix is not None and len(vix) >= 6:
            last, before = float(vix["Close"].iloc[-1]), float(vix["Close"].iloc[-6])
            if last >= VIX_SHOCK and last / before - 1 >= VIX_JUMP:
                return f"VIX a {last:.0f} (+{(last / before - 1) * 100:.0f}% em 5 sessões)"
    except Exception:  # noqa: BLE001 - no VIX, no trigger
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--contexto")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--gatilho", action="store_true", help="Only say whether a grave event calls for a view now")
    ap.add_argument("--motivo", default="", help="Why this view was asked for outside the schedule")
    ap.add_argument("--avaliar", action="store_true", help="Only score the past views against the market")
    a = ap.parse_args()
    if a.avaliar:
        run_evaluation()
        return 0
    if a.gatilho:
        why = shock(datetime.now(timezone.utc))
        print(f"VISAO_EXTRA={why}" if why else "VISAO_EXTRA=", flush=True)
        return 0
    frame = context.load(a.contexto) if a.contexto else context.download("2y", None, None)
    ctx = context.on(frame, datetime.now(timezone.utc).date())
    raw = ask({**our_numbers(ctx), "manchetes_ultimas_2_semanas": headlines()})
    vision = clean(raw or {})
    if not vision:
        print("Visão: sem visão válida esta semana; fica a anterior", flush=True)
        return 0
    vision["at"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    if a.motivo:
        vision["extraordinaria"] = a.motivo[:300]
    if a.dry_run:
        print(json.dumps({k: vision[k] for k in ("setores", "industrias", "fatores", "horizontes")}, ensure_ascii=False))
        return 0
    VISION_PATH.write_text(json.dumps(vision, ensure_ascii=False, indent=1) + "\n")
    try:
        hist = json.loads(HISTORY_PATH.read_text())
    except (OSError, ValueError):
        hist = []
    hist.append(vision)
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(hist[-HISTORY_KEEP:], ensure_ascii=False, indent=1) + "\n")
    print("Visão: guardada no repositório privado", flush=True)
    from discord_post import route, send

    return send([card(vision)], {}, **(route("agentes") or {}))


if __name__ == "__main__":
    sys.exit(main())
