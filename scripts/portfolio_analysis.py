"""Claude's analysis of a member's portfolio, started from the portfolio page.

The Worker stores what it knows under analysis-req:<rid> in KV (holdings, cash per account,
verdicts, safety scores, news, calendar, market context, the agents' proposals) and starts
portfolio-analysis.yml with the id. This script asks Claude (Claude Code CLI with the owner's
subscription, CLAUDE_CODE_OAUTH_TOKEN) for a structured analysis and stores it under
analysis-res:<rid>, which the page shows.

    uv run python scripts/portfolio_analysis.py <rid>
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import discord_bot

import agents
import claude_usage

MODEL = "sonnet"
KEEP_DAYS = 90

PROMPT = agents.recipe_prompt("analise_carteira")  # private: agents/receita/prompts/analise_carteira.md


def extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0)) if match else {}


def ask_claude(payload: dict) -> tuple[dict | None, str]:
    token = re.sub(r"\s+", "", os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
    if not token or not shutil.which("claude") or not PROMPT:
        return None, "Claude indisponível (sem token ou sem o Claude Code)."
    try:
        proc = subprocess.run(["claude", "-p", "--output-format", "json", "--model", MODEL, "--max-turns", "1"],
                              input=PROMPT + json.dumps(payload, ensure_ascii=False, indent=1),
                              capture_output=True, text=True, timeout=600, env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token})
        out = json.loads(proc.stdout or "{}")
        claude_usage.record("carteira", MODEL, out)
    except (subprocess.TimeoutExpired, ValueError) as e:
        return None, f"O Claude não respondeu: {e}"
    if proc.returncode != 0 or out.get("is_error"):
        return None, f"O Claude falhou: {(out.get('result') or proc.stderr or '')[:300]}"
    try:
        result = extract_json(out.get("result", ""))
    except ValueError:
        result = {}
    if not result.get("resumo"):
        return None, "A resposta do Claude não veio no formato esperado."
    return result, ""


def main() -> int:
    rid = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not re.fullmatch(r"[0-9a-f]{24}", rid):
        print("Uso: portfolio_analysis.py <id de 24 hex>", file=sys.stderr)
        return 2
    payload = discord_bot.kv_get(f"analysis-req:{rid}")
    if not payload:
        print(f"::error title=Análise::pedido {rid} não encontrado no KV")
        return 1
    result, error = ask_claude(payload)
    done = {"status": "done" if result else "error", "at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "model": MODEL, "result": result, "error": error or None}
    discord_bot.kv_put(f"analysis-res:{rid}", done, ttl_days=KEEP_DAYS)
    print(f"::notice title=Análise::{'pronta' if result else 'falhou: ' + error}")
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
