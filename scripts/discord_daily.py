"""Post a daily market-breadth summary to a Discord channel via webhook.

Runs uptrend-analyzer (and market-breadth-analyzer when its data host is
reachable), turns the JSON reports into a short Portuguese message and posts
it to DISCORD_WEBHOOK_URL. Use --dry-run to print the message instead.

    uv run python scripts/discord_daily.py --dry-run
    DISCORD_WEBHOOK_URL=... uv run python scripts/discord_daily.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
DISCORD_LIMIT = 2000

BREADTH_DETAIL_URL = "https://tradermonty.github.io/market-breadth-analysis/market_breadth_data.csv"
BREADTH_SUMMARY_URL = "https://tradermonty.github.io/market-breadth-analysis/market_breadth_summary.csv"

ZONES_PT = {
    "Strong Bull": ("🟢", "Muito forte"),
    "Strong": ("🟢", "Forte"),
    "Bull": ("🟢", "Positivo"),
    "Healthy": ("🟢", "Saudável"),
    "Neutral": ("🟡", "Neutro"),
    "Cautious": ("🟠", "Cauteloso"),
    "Weakening": ("🟠", "A enfraquecer"),
    "Bear": ("🔴", "Negativo"),
    "Critical": ("🔴", "Crítico"),
}

EXPOSURE_PT = {
    "Full Exposure (100%)": "100%",
    "Normal Exposure (80-100%)": "80-100%",
    "Reduced Exposure (60-80%)": "60-80%",
    "Defensive (30-60%)": "30-60% (defensivo)",
    "Capital Preservation (0-30%)": "0-30% (preservar capital)",
}

WARNINGS_PT = {
    "late_cycle": "Matérias-primas a liderar: sinal típico de fim de ciclo",
    "high_spread": "Mercado muito seletivo: grande diferença entre setores",
    "divergence": "Setores do mesmo grupo a divergir: risco escondido",
}

ACTIONS_PT = {
    "Strong Bull": "Exposição total; privilegiar momentum e crescimento.",
    "Bull": "Operar normalmente com stops habituais.",
    "Neutral": "Reduzir posições 20-30%, só os setores mais fortes, stops mais apertados.",
    "Cautious": "Reduzir exposição, só setups de qualidade A, subir o cash.",
    "Bear": "Preservar capital; evitar entradas novas.",
}

SECTORS_PT = {
    "Energy": "Energia",
    "Healthcare": "Saúde",
    "Technology": "Tecnologia",
    "Communication Services": "Comunicações",
    "Consumer Cyclical": "Consumo cíclico",
    "Consumer Defensive": "Consumo defensivo",
    "Financial": "Financeiro",
    "Industrials": "Industrial",
    "Real Estate": "Imobiliário",
    "Basic Materials": "Materiais",
    "Utilities": "Utilities",
}


def run_skill(script: str, out_dir: Path, *args: str) -> dict | None:
    """Run a skill script and return its newest JSON report, or None on failure."""
    before = set(out_dir.glob("*.json"))
    proc = subprocess.run(
        [sys.executable, str(REPO / script), *args, "--output-dir", str(out_dir)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"[aviso] {script} falhou: {proc.stdout.strip().splitlines()[-1:]}", file=sys.stderr)
        return None
    new = sorted(set(out_dir.glob("*.json")) - before, key=lambda p: p.stat().st_mtime)
    reports = [p for p in new if "history" not in p.name]
    return json.loads(reports[-1].read_text()) if reports else None


def zone_label(zone: str) -> str:
    icon, name = ZONES_PT.get(zone, ("⚪", zone))
    return f"{icon} {name}"


def sector(name: str) -> str:
    return SECTORS_PT.get(name, name)


def build_message(uptrend: dict, breadth: dict | None) -> str:
    comp = uptrend["composite"]
    mb = uptrend["components"]["market_breadth"]
    sp = uptrend["components"]["sector_participation"]
    details = sorted(sp["sector_details"], key=lambda s: s["ratio"], reverse=True)
    as_of = uptrend["metadata"]["latest_data_date"]

    lines = [
        f"📊 **Dicas do dia — breadth do mercado (dados de {as_of})**",
        "",
        f"**Uptrend score:** {comp['composite_score']}/100 · {zone_label(comp['zone'])}",
        f"**Exposição sugerida a ações:** {EXPOSURE_PT.get(comp['exposure_guidance'], comp['exposure_guidance'])}",
    ]
    if breadth:
        bc = breadth["composite"]
        lines.append(
            f"**Breadth score:** {bc['composite_score']}/100 · {zone_label(bc['zone'])} "
            f"(exposição {bc['exposure_guidance']})"
        )
    lines += [
        "",
        f"📈 {mb['ratio_pct']}% das ações em tendência de alta "
        f"(média 10 dias: {mb['ma_10_pct']}%, tendência {'a subir' if mb['trend'] == 'up' else 'a descer'})",
        f"🏭 {sp['uptrend_count']}/{sp['total_sectors']} setores em tendência de alta",
        "",
        "**Mais fortes:** " + ", ".join(f"{sector(s['sector'])} {s['ratio_pct']}%" for s in details[:3]),
        "**Mais fracos:** " + ", ".join(f"{sector(s['sector'])} {s['ratio_pct']}%" for s in details[-3:][::-1]),
    ]

    warnings = [WARNINGS_PT[w["flag"]] for w in comp.get("active_warnings", []) if w["flag"] in WARNINGS_PT]
    if warnings:
        lines += ["", "⚠️ **Avisos:**"] + [f"• {w}" for w in warnings]

    action = ACTIONS_PT.get(comp["zone"])
    if action:
        lines += ["", f"🎯 **O que fazer:** {action}"]

    lines += ["", "_Ferramenta de análise educativa, não é aconselhamento financeiro._"]
    message = "\n".join(lines)
    return message[: DISCORD_LIMIT - 1] + "…" if len(message) > DISCORD_LIMIT else message


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print the message instead of posting")
    args = parser.parse_args()

    out_dir = REPO / "reports" / f"discord-{date.today().isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    uptrend = run_skill("skills/uptrend-analyzer/scripts/uptrend_analyzer.py", out_dir)
    if uptrend is None:
        print("Sem dados do uptrend-analyzer; nada enviado.", file=sys.stderr)
        return 1
    breadth = run_skill(
        "skills/market-breadth-analyzer/scripts/market_breadth_analyzer.py",
        out_dir,
        "--detail-url",
        BREADTH_DETAIL_URL,
        "--summary-url",
        BREADTH_SUMMARY_URL,
    )

    message = build_message(uptrend, breadth)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if args.dry_run:
        print(message)
        return 0
    if not webhook:
        print("DISCORD_WEBHOOK_URL não definido; usa --dry-run para só ver a mensagem.", file=sys.stderr)
        return 1

    resp = requests.post(webhook, json={"content": message}, timeout=30)
    if resp.status_code >= 300:
        print(f"Discord respondeu {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return 1
    print("Mensagem enviada para o Discord.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
