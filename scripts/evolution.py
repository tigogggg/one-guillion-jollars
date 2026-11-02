"""History of the agents' pre-training: one entry per run, for the portal's evolution chart.

Each pre-training (GitHub Actions or a local run) appends what it chose for every strategy: 20-year
return, maximum drawdown, Sharpe, exam points (against the original parameters), whether it adopted new
parameters or kept the previous ones, and which shared rules are switched on. The daily agents run
publishes a compact copy to the Worker's KV (portal:evolution).

    uv run python scripts/evolution.py --reconstruir   # rebuild from the git history of agents/pretrain.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import agents

HISTORY_PATH = agents.REPO_AGENTS / "pretrain_history.json"
KEEP = 200
MIN_TRAIN_YEARS = 15  # shorter test runs are left out, they are not comparable


def rules_on(chosen: dict) -> list[str]:
    return sorted(k for k in agents.OFFABLE if (chosen or {}).get(k))


def entry(report: dict, at: str, source: str, seed: str = "") -> dict | None:
    """The comparable summary of one pre-training report, or None for a short test run."""
    train = report.get("train") or []
    if len(train) == 2 and (int(train[1][:4]) - int(train[0][:4])) < MIN_TRAIN_YEARS:
        return None
    rows = {}
    for s, r in (report.get("strategies") or {}).items():
        if "year" not in r:
            continue
        y, v = r["year"], r.get("valid") or {}
        rows[s] = {
            "ret": round(y.get("return", 0) * 100, 1), "dd": round(y.get("max_drawdown", 0) * 100, 1),
            "sharpe": round(y.get("sharpe") or 0, 2), "exam": (v.get("best") or {}).get("points"),
            "exam0": (v.get("default") or {}).get("points"), "adopted": bool(r.get("adopted")),
            "kept": bool(r.get("kept_previous")), "on": rules_on(r.get("chosen") or {}),
            "params": {k: val for k, val in (r.get("chosen") or {}).items() if k != "por_zona"},
            "zones": sorted((r.get("chosen") or {}).get("por_zona") or {}),
        }
    return {"at": at, "source": source, "seed": seed, "train": train, "valid": report.get("valid"), "strategies": rows} if rows else None


def load() -> list[dict]:
    try:
        return json.loads(HISTORY_PATH.read_text())
    except (OSError, ValueError):
        return []


def append(report: dict, seed: str = "") -> None:
    e = entry(report, datetime.now(timezone.utc).isoformat(timespec="minutes"),
              "actions" if os.environ.get("GITHUB_ACTIONS") else "local", seed)
    if not e:
        return
    e["perfil"] = report.get("perfil", "equilibrado")
    history = load() + [e]
    HISTORY_PATH.write_text(json.dumps(history[-KEEP:], ensure_ascii=False, indent=1) + "\n")


def compact(history: list[dict]) -> dict:
    """For the portal: per run its date and source; per strategy its series and what changed between runs.
    Only the balanced profile's runs (the others train for other investors and are not one series). The exam points
    of runs before the investor profiles (no "perfil") were league points, another scale: left out of the exam series."""
    history = [h for h in history if h.get("perfil", "equilibrado") == "equilibrado"]
    runs = [{"at": h["at"], "source": h.get("source", "")} for h in history]
    series: dict[str, list] = {}
    changes: dict[str, list] = {}
    for i, h in enumerate(history):
        for s, r in h["strategies"].items():
            row = {k: r.get(k) for k in ("ret", "dd", "sharpe", "exam", "exam0", "adopted", "kept")}
            if "perfil" not in h:
                row["exam"] = row["exam0"] = None
            series.setdefault(s, [None] * len(history))[i] = row
            prev = next((history[j]["strategies"].get(s) for j in range(i - 1, -1, -1) if s in history[j]["strategies"]), None)
            if prev is None:
                continue
            on, off = sorted(set(r["on"]) - set(prev["on"])), sorted(set(prev["on"]) - set(r["on"]))
            moved = sorted(k for k, v in r["params"].items() if k not in agents.OFFABLE and prev["params"].get(k) not in (None, v))
            if on or off or moved:
                changes.setdefault(s, []).append({"run": i, "on": on, "off": off, "moved": moved[:6]})
    for s in series:  # pad runs added before the strategy existed
        series[s] += [None] * (len(history) - len(series[s]))
    return {"runs": runs, "series": series, "changes": changes}


def rebuild() -> list[dict]:
    """Entries from every commit of pretrain.json in the private data repo checked out at agents/ (oldest first)."""
    repo = agents.REPO_AGENTS
    log = subprocess.run(["git", "log", "--reverse", "--format=%H %aI", "--", "pretrain.json"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout.split("\n")
    out = []
    for line in filter(None, log):
        sha, at = line.split(" ", 1)
        try:
            report = json.loads(subprocess.run(["git", "show", f"{sha}:pretrain.json"], cwd=repo, capture_output=True,
                                               text=True, check=True).stdout)
        except (subprocess.CalledProcessError, ValueError):
            continue
        e = entry(report, at, "actions", "")
        if e and (not out or e["strategies"] != out[-1]["strategies"]):
            out.append(e)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reconstruir", action="store_true", help="Rebuild agents/pretrain_history.json from git")
    a = parser.parse_args()
    if a.reconstruir:
        history = rebuild()
        HISTORY_PATH.write_text(json.dumps(history[-KEEP:], ensure_ascii=False, indent=1) + "\n")
        print(f"{len(history)} execuções no histórico")
    return 0


if __name__ == "__main__":
    sys.exit(main())
