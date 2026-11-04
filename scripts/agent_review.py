"""Claude reviews the agents and proposes parameters; the pre-training decides.

After each pre-training, Claude gets every strategy's results (20 years, exam year, crises, Monte Carlo,
what each parameter is worth, what the final phase switched on, zone parameters), its live record and
shadow copies, its rules and its tunable parameters with their bounds. It writes an assessment per agent
and proposes up to MAX_PROPOSALS parameter sets per strategy, each with the hypothesis behind it.

The proposals are validated (known parameters only, clamped to their bounds) and saved in
agents/claude_review.json. The next pre-training adds them to its candidates: they are adopted only if
they pass the same tests as any other candidate (65% of the training years, plateau, crises, exam).
Nothing is applied because Claude says so. A summary card goes to the agents channel.

    uv run python scripts/agent_review.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date

import claude_usage
import agents
import strategy_docs
from discord_post import BLUE, detail_posts, embed, route, send

MODEL = "sonnet"
MAX_PROPOSALS = 3
REVIEW_PATH = agents.REPO_AGENTS / "claude_review.json"
REPORT_PATH = agents.REPO_AGENTS / "pretrain.json"

PROMPT = agents.recipe_prompt("revisao_agentes")  # private: agents/receita/prompts/revisao_agentes.md


def tunables(strategy: str) -> dict[str, tuple]:
    from pretrain import SHARED_IN_BACKTEST

    own = [k for k in agents.STRATEGIES[strategy][2] if k in agents.PARAM_BOUNDS]
    shared = SHARED_IN_BACKTEST if strategy != "indexador" else []
    return {k: agents.bounds_of(strategy, k) for k in own + shared}


def payload() -> dict:
    pre = json.loads(REPORT_PATH.read_text()) if REPORT_PATH.exists() else {}
    state, config = agents.load_state(), agents.load_config()
    live = {}
    for cfg in agents.active_agents(config):
        port = state["agents"].get(cfg["id"])
        if port:
            st = agents.live_stats(port)
            live.setdefault(cfg["strategy"], []).append({
                "agent": cfg["name"], "sessions": st["sessions"], "return_pct": round(st["return"] * 100, 2),
                "sharpe": round(st["sharpe"], 2), "points": port["points"], "trades": len(port["trades"]),
                "shadows": [{"changes": sh["changes"], "ret": sh.get("ret"), "parent_ret": sh.get("parent_ret"), "sessions": sh.get("sessions")}
                            for sh in state.get("shadows", {}).get(cfg["id"], [])],
            })
    out = {"periodo": {"treino": pre.get("train"), "exame": pre.get("valid")}, "estrategias": {}}
    for strategy, r in pre.get("strategies", {}).items():
        if "year" not in r or strategy == "indexador":
            continue
        idea, buys, sells, size = strategy_docs.describe(strategy, r.get("chosen") or {})
        y, v = r["year"], r.get("valid", {})
        out["estrategias"][strategy] = {
            "regras": {"ideia": idea, "compra": buys, "vende": sells, "tamanho": size},
            "parametros_atuais": r.get("chosen"),
            "ajustaveis": {k: {"min": lo, "max": hi, "tipo": kind.__name__, "desligavel": k in agents.OFFABLE}
                           for k, (lo, hi, kind) in tunables(strategy).items()},
            "vinte_anos": {k: y.get(k) for k in ("return", "max_drawdown", "sharpe", "trades", "bankrupt", "trade_stats")},
            "exame": {"escolhidos": v.get("best"), "originais": v.get("default")},
            "pontos_por_ano_treino": (r.get("train", {}).get("best") or {}).get("years"),
            "crises": r.get("crises"), "monte_carlo": r.get("monte_carlo"), "por_zona": r.get("by_zone"),
            "importance": r.get("importance"), "fase_final": r.get("greedy"),
            "zonas": {z: {"ficou": d.get("kept"), "muda": d.get("diff")} for z, d in (r.get("zones") or {}).items()},
            "ao_vivo": live.get(strategy, []),
        }
    out["indexador_20_anos"] = {k: pre.get("strategies", {}).get("indexador", {}).get("year", {}).get(k)
                                for k in ("return", "max_drawdown", "sharpe")}
    return out


def ask(data: dict) -> tuple[dict | None, str]:
    token = re.sub(r"\s+", "", os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
    if not token or not shutil.which("claude") or not PROMPT:
        return None, "Claude indisponível (sem token ou sem o Claude Code)."
    try:
        proc = subprocess.run(["claude", "-p", "--output-format", "json", "--model", MODEL, "--max-turns", "1"],
                              input=PROMPT + json.dumps(data, ensure_ascii=False, default=str), capture_output=True, text=True,
                              timeout=900, env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token})
        out = json.loads(proc.stdout or "{}")
        claude_usage.record("agent_review", MODEL, out)
        if proc.returncode != 0 or out.get("is_error"):
            return None, f"O Claude falhou: {(out.get('result') or proc.stderr or '')[:300]}"
        match = re.search(r"\{.*\}", out.get("result", ""), re.DOTALL)
        result = json.loads(match.group(0)) if match else {}
    except (subprocess.TimeoutExpired, ValueError) as e:
        return None, f"O Claude não respondeu bem: {e}"
    if not isinstance(result.get("estrategias"), dict):
        return None, "A resposta do Claude não veio no formato esperado."
    return result, ""


def clean(strategy: str, params: dict) -> dict:
    """Only this strategy's tunable parameters, within their bounds (0 switches a shared rule off)."""
    allowed, out = tunables(strategy), {}
    for k, v in (params or {}).items():
        if k not in allowed or not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        lo, hi, kind = allowed[k]
        if v == 0 and k in agents.OFFABLE:
            out[k] = 0
            continue
        v = min(hi, max(lo, v))
        out[k] = int(round(v)) if kind is int else round(float(v), 4)
    return out


def validate(result: dict) -> dict:
    review = {"date": date.today().isoformat(), "model": MODEL, "geral": str(result.get("geral", ""))[:1500], "estrategias": {}}
    for strategy, r in (result.get("estrategias") or {}).items():
        if strategy not in agents.STRATEGIES or not isinstance(r, dict):
            continue
        proposals = []
        for prop in (r.get("propostas") or [])[:MAX_PROPOSALS]:
            params = clean(strategy, (prop or {}).get("params"))
            if params:
                proposals.append({"params": params, "hipotese": str(prop.get("hipotese", ""))[:400], "espera": str(prop.get("espera", ""))[:300]})
        review["estrategias"][strategy] = {
            "parecer": str(r.get("parecer", ""))[:1200], "fortes": [str(x)[:200] for x in (r.get("fortes") or [])][:5],
            "fracos": [str(x)[:200] for x in (r.get("fracos") or [])][:5], "manter": bool(r.get("manter", True)),
            "propostas": proposals, "regras_novas": [str(x)[:300] for x in (r.get("regras_novas") or [])][:5],
        }
    return review


def card(review: dict) -> dict:
    config = agents.load_config()
    names = {c["strategy"]: f"{c['emoji']} {c['name']}" for c in agents.active_agents(config)}
    lines = [f"{names.get(s, s)}{'' if r['manter'] else ' · ⚠️ a repensar'}: {r['parecer'][:180]}" for s, r in review["estrategias"].items()]
    n = sum(len(r["propostas"]) for r in review["estrategias"].values())
    return embed("🤖 Revisão dos agentes pelo Claude", review["geral"][:1500], color=BLUE,
                 fields=[("Por agente", "\n".join(lines)[:1024] or "—"),
                         ("🧪 Propostas para o próximo retreino",
                          f"{n} conjuntos de parâmetros, cada um com a sua hipótese. Entram no pré-treino como candidatos e só são "
                          "aplicados se passarem os mesmos testes que os outros (anos, robustez, crises, exame).")],
                 footer="O Claude propõe; o backtest decide. Detalhe no portal, separador ⏪ Pré-treino.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print the review instead of saving and posting it")
    args = parser.parse_args()
    data = payload()
    if not data["estrategias"]:
        print("Sem resultados do pré-treino para rever.", file=sys.stderr)
        return 1
    result, error = ask(data)
    if not result:
        print(f"::error title=Revisão do Claude::{error}")
        return 1
    review = validate(result)
    n = sum(len(r["propostas"]) for r in review["estrategias"].values())
    print(f"::notice title=Revisão do Claude::{len(review['estrategias'])} estratégias, {n} propostas de parâmetros válidas")
    if args.dry_run:
        print(json.dumps(review, ensure_ascii=False, indent=1))
        return 0
    REVIEW_PATH.write_text(json.dumps(review, ensure_ascii=False, indent=1) + "\n")
    if not detail_posts():  # the review is in the portal (each strategy's sheet)
        return 0
    return send([card(review)], **route("agentes"), replace="🤖 Revisão dos agentes pelo Claude")


if __name__ == "__main__":
    sys.exit(main())
