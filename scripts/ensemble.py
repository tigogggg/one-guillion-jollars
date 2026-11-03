"""Do we gain anything from running so many agents at once? From a pre-training report (each strategy's 20 years with
the parameters it trades with), year by year against the S&P 500 (the Indexador):

- each strategy alone: in how many years it beat the index;
- all together: the same money in every strategy, rebalanced each January (diversification);
- the leaders: each year, the TOP strategies of the year before (does picking last year's winners help?);
- how alike they are: the average correlation of their weekly returns, and the "independent bets" it leaves
  (N strategies that move together are worth fewer than N).

The 20 years include the years the parameters were chosen on, so every line is optimistic; the comparison between
them is the point. Results go to the private repo (agents/lab/conjunto-<date>.json), never to the Actions log.

    uv run python scripts/ensemble.py            # every profile's latest report
"""

from __future__ import annotations

import json
import sys
from datetime import date

import numpy as np
import pandas as pd

import agents

TOP = 3


def yearly(curve: list) -> pd.Series:
    """Calendar-year returns from a curve of (date, % since the start); the first and last years are partial."""
    s = pd.Series([1 + p / 100 for _, p in curve], index=pd.to_datetime([d for d, _ in curve]))
    ends = s.groupby(s.index.year).last()
    starts = ends.shift(1)
    starts.iloc[0] = s.iloc[0]
    return ends / starts - 1


def analyse(report: dict) -> dict:
    curves = {s: r["curve"] for s, r in report.get("strategies", {}).items() if r.get("curve")}
    if "indexador" not in curves or len(curves) < 3:
        return {}
    years = pd.DataFrame({s: yearly(c) for s, c in curves.items()}).dropna(how="all")
    index = years.pop("indexador")
    years = years.dropna(axis=1, thresh=len(years) // 2)
    beats = {s: int((years[s] > index).sum()) for s in years}
    together = years.mean(axis=1)
    leaders = pd.Series(np.nan, index=years.index)
    for i in range(1, len(years)):
        prev = years.iloc[i - 1].dropna().sort_values(ascending=False)
        pick = list(prev.index[:TOP])
        leaders.iloc[i] = years.iloc[i][pick].mean()
    weekly = pd.DataFrame({s: pd.Series([1 + p / 100 for _, p in c], index=pd.to_datetime([d for d, _ in c]))
                           for s, c in curves.items() if s in years}).resample("W").last().pct_change().dropna(how="all")
    corr = weekly.corr().to_numpy()
    n = len(corr)
    off = corr[~np.eye(n, dtype=bool)] if n > 1 else np.array([])
    mean_corr = float(np.nanmean(off)) if off.size and not np.isnan(off).all() else 0.0
    best = max(beats, key=beats.get)

    def row(series: pd.Series) -> dict:
        s = series.dropna()
        growth = float((1 + s).prod() ** (1 / len(s)) - 1) if len(s) else 0.0
        return {"beats_index": int((s > index[s.index]).sum()), "years": len(s), "yearly_growth": round(growth, 4),
                "worst_year": round(float(s.min()), 4) if len(s) else None}

    return {
        "years": [int(y) for y in years.index], "index": row(index), "together": row(together),
        "leaders": row(leaders), "best_alone": {"strategy": best, **row(years[best])},
        "alone": {s: row(years[s]) for s in years}, "mean_correlation": round(mean_corr, 3),
        "independent_bets": round(n / (1 + (n - 1) * mean_corr), 1) if n else 0, "strategies": n,
    }


def summary_lines(a: dict) -> list[str]:
    """For the pre-training report (pt-PT)."""
    if not a:
        return []
    y = a["index"]["years"]

    def fmt(k: str, label: str) -> str:
        r = a[k]
        return f"{label}: bate o S&P 500 em {r['beats_index']}/{r['years']} anos · {r['yearly_growth'] * 100:+.1f}%/ano"

    b = a["best_alone"]
    return [fmt("together", "Todos juntos (mesmo dinheiro em cada um)"), fmt("leaders", f"Os {TOP} melhores do ano anterior"),
            f"Melhor sozinho ({b['strategy']}): bate o S&P 500 em {b['beats_index']}/{b['years']} anos · "
            f"{b['yearly_growth'] * 100:+.1f}%/ano",
            f"S&P 500: {a['index']['yearly_growth'] * 100:+.1f}%/ano em {y} anos",
            f"Parecidos entre si: correlação média {a['mean_correlation']:.2f}, ou seja {a['strategies']} estratégias "
            f"valem por cerca de {a['independent_bets']} apostas independentes"]


def main() -> int:
    out = {}
    for profile in agents.PROFILES:
        path = agents.pretrain_path(profile)
        if path.exists():
            out[profile] = analyse(json.loads(path.read_text()))
    target = agents.REPO_AGENTS / "lab" / f"conjunto-{date.today().isoformat()}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"Análise do conjunto guardada no repositório privado ({target.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
