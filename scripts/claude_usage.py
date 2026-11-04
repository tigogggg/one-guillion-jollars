"""How much of the Claude subscription the routines use: every `claude -p` call leaves one small record (script, model,
tokens, turns, time, the API-equivalent cost the CLI reports) in the private repo, one file per call so that
workflows running at the same time never collide (agents/lab/uso_claude/<YYYY-MM>/<time>-<script>.json).
Usage figures stay out of the public Actions log.

    uv run python scripts/claude_usage.py [--meses 3]      # the monthly totals by routine (locally)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime

import agents

USAGE_DIR = agents.REPO_AGENTS / "lab" / "uso_claude"
TOKENS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def record(script: str, model: str, out: dict | None, now: datetime | None = None) -> None:
    """Keep one call's usage (out: the JSON `claude -p --output-format json` printed). Never fails the caller."""
    try:
        if not isinstance(out, dict) or not agents.REPO_AGENTS.is_dir():
            return
        now = now or datetime.now(UTC)
        usage = out.get("usage") or {}
        rec = {"at": now.isoformat(timespec="seconds"), "script": script, "model": model,
               "workflow": os.environ.get("GITHUB_WORKFLOW", "local"), "erro": bool(out.get("is_error")),
               **{k: int(usage.get(k) or 0) for k in TOKENS},
               "turns": int(out.get("num_turns") or 0), "ms": int(out.get("duration_ms") or 0),
               "custo_usd": round(float(out.get("total_cost_usd") or 0), 4),
               "por_modelo": {m: {k: v for k, v in (u or {}).items() if isinstance(v, int | float)}
                              for m, u in (out.get("modelUsage") or {}).items()}}
        folder = USAGE_DIR / now.strftime("%Y-%m")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{now.strftime('%d-%H%M%S%f')}-{script}-{os.getpid()}.json"
        path.write_text(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - bookkeeping only
        return


def summary(months: int = 3) -> dict:
    """Totals per month and routine: calls, tokens (fresh input, cache, output), cost the API would charge."""
    out: dict = {}
    folders = sorted(p for p in USAGE_DIR.glob("*") if p.is_dir())[-months:] if USAGE_DIR.is_dir() else []
    for folder in folders:
        by = defaultdict(lambda: defaultdict(float))
        for f in folder.glob("*.json"):
            try:
                r = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            row = by[r.get("script", "?")]
            row["chamadas"] += 1
            for k in (*TOKENS, "custo_usd", "turns"):
                row[k] += r.get(k) or 0
        out[folder.name] = {s: {k: round(v, 2) for k, v in row.items()} for s, row in sorted(by.items())}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--meses", type=int, default=3)
    a = ap.parse_args()
    for month, rows in summary(a.meses).items():
        total = sum(r["custo_usd"] for r in rows.values())
        print(f"\n{month}: {sum(int(r['chamadas']) for r in rows.values())} chamadas, ~{total:.2f} USD equivalentes na API")
        for s, r in sorted(rows.items(), key=lambda kv: -kv[1]["custo_usd"]):
            tok = sum(r[k] for k in TOKENS)
            print(f"  {s:<22} {int(r['chamadas']):>4} chamadas  {tok / 1e6:6.2f} M tokens  {r['custo_usd']:7.2f} USD"
                  f"  ({100 * r['custo_usd'] / total if total else 0:.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
