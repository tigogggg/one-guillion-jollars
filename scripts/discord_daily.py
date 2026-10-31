"""Post a daily market-breadth summary to a Discord channel via webhook.

Runs uptrend-analyzer (and market-breadth-analyzer when its data host is
reachable) and vcp-screener (with FMP_API_KEY), turns the JSON reports into a
Portuguese card with a sector chart and posts it to DISCORD_WEBHOOK_URL. Use
--dry-run to print it and save the chart to reports/preview/.

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

import discord_bot
import earnings
import macro
from charts import sector_chart
from discord_post import BLUE, ZONE_COLORS, embed, post_message, route

REPO = Path(__file__).resolve().parents[1]
# Shared with the intraday alerts through the Actions cache.
WATCHLIST_DIR = REPO / "state" / "watchlist"

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


VCP_GOOD_RATINGS = ("Textbook VCP", "Strong VCP", "Good VCP")
VCP_MAX_SHOWN = 5
# FMP free tier: 250 calls/day and no batch quotes, so one history call per stock.
VCP_FREE_TIER_ARGS = ("--history-quotes", "--max-universe", "240", "--api-budget", "246")


def run_skill(script: str, out_dir: Path, *args: str) -> dict | None:
    """Run a skill script and return its newest JSON report, or None on failure."""
    before = set(out_dir.glob("*.json"))
    proc = subprocess.run(
        [sys.executable, str(REPO / script), *args, "--output-dir", str(out_dir)],
        capture_output=True,
        text=True,
    )
    problems = [ln for ln in proc.stderr.splitlines() if "ERROR" in ln or "WARN" in ln]
    for line in problems[:20]:
        print(f"[{Path(script).parent.parent.name}] {line}", file=sys.stderr)
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


def vcp_lines(vcp: dict, zone: str) -> list[str]:
    picks = [r for r in vcp["results"] if r.get("rating") in VCP_GOOD_RATINGS]
    picks.sort(key=lambda r: (not r.get("entry_ready"), -r["composite_score"]))
    lines = ["", f"🔍 **Setups VCP no S&P 500:** {len(picks)} com qualidade Good ou melhor"]
    funnel = vcp.get("metadata", {}).get("funnel", {})
    if funnel:
        calls = vcp["metadata"].get("api_stats", {}).get("api_calls_made", "?")
        lines.append(
            f"_{funnel.get('universe', 0)} ações → {funnel.get('pre_filter_passed', 0)} no pré-filtro"
            f" → {funnel.get('trend_template_passed', 0)} em Stage 2"
            f" → {funnel.get('vcp_candidates', 0)} padrões · {calls} chamadas FMP_"
        )
        if funnel.get("universe") and not funnel.get("pre_filter_passed"):
            lines.append("⚠️ Nenhuma ação passou o pré-filtro: provavelmente a FMP não devolveu dados.")
    if not picks:
        return lines + ["Nenhum setup de qualidade hoje."]
    for r in picks[:VCP_MAX_SHOWN]:
        piv = r.get("pivot_proximity", {})
        parts = [f"• **{r['symbol']}** ${r['price']:.2f}", f"{r['rating']} ({r['composite_score']:.0f})"]
        if piv.get("pivot_price"):
            parts.append(f"pivot ${piv['pivot_price']:.2f}")
        if piv.get("stop_loss_price"):
            parts.append(f"stop ${piv['stop_loss_price']:.2f} (risco {piv.get('risk_pct', 0):.1f}%)")
        if r.get("entry_ready"):
            parts.append("✅ perto da entrada")
        lines.append(" · ".join(parts))
    if zone in ("Cautious", "Bear"):
        lines.append("_Mercado fraco: lista só para observar, não para entrar._")
    else:
        lines.append("_Lista de observação._")
    return lines


def build_post(
    uptrend: dict, breadth: dict | None, vcp: dict | None = None, title: str = "📊 Dicas do dia", macro_line: str | None = None
) -> tuple[list[dict], dict]:
    """The tips as one embed coloured by market zone, with the sector chart attached."""
    comp = uptrend["composite"]
    mb = uptrend["components"]["market_breadth"]
    sp = uptrend["components"]["sector_participation"]
    details = sorted(sp["sector_details"], key=lambda s: s["ratio"], reverse=True)
    as_of = uptrend["metadata"]["latest_data_date"]

    lines = [
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

    if macro_line:
        lines += ["", macro_line]

    action = ACTIONS_PT.get(comp["zone"])
    if action:
        lines += ["", f"🎯 **O que fazer:** {action}"]

    if vcp:
        lines += vcp_lines(vcp, comp["zone"])

    chart = sector_chart([(sector(s["sector"]), s["ratio_pct"], s["trend"] == "Up") for s in details])
    card = embed(
        f"{title}: breadth do mercado (dados de {as_of})",
        "\n".join(lines),
        color=ZONE_COLORS.get(comp["zone"], BLUE),
        image="setores.png",
    )
    return [card], {"setores.png": chart}


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

    # Before the VCP screen, which spends almost all of the FMP free tier's daily calls.
    macro_line = macro.today_tomorrow_line()

    vcp = None
    if os.environ.get("FMP_API_KEY"):
        vcp = run_skill(
            "skills/vcp-screener/scripts/screen_vcp.py",
            out_dir,
            *VCP_FREE_TIER_ARGS,
        )
    else:
        print("[aviso] FMP_API_KEY não definido; secção VCP omitida.", file=sys.stderr)

    if vcp:
        calls = vcp.get("metadata", {}).get("api_stats", {}).get("api_calls_made")
        if calls is not None and os.environ.get("GITHUB_ACTIONS"):  # read back by /limites
            print(f"::notice title=FMP::{calls} chamadas à API no ecrã VCP (limite do plano gratuito: 250/dia)")
        # The intraday alerts read today's VCP pivots from here.
        WATCHLIST_DIR.mkdir(parents=True, exist_ok=True)
        (WATCHLIST_DIR / "vcp-latest.json").write_text(json.dumps(vcp))

    embeds, files = build_post(uptrend, breadth, vcp, macro_line=macro_line)
    channel = route("mercado")
    message = post_message(embeds, files, dry_run=args.dry_run, **channel)
    if message is None:
        return 1
    if not args.dry_run and not channel:  # a day thread only while everything shares one channel
        today = date.today()
        discord_bot.start_daily_thread(message, f"📅 {earnings.label(today)} · análise do dia", today)
    return 0

if __name__ == "__main__":
    sys.exit(main())
