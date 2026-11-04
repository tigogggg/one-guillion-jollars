"""Commit and push agents/ (the private repo) without mixing two runs' agents.

Several jobs write the agents' config.json and state.json (the rounds every 10 minutes, the daily league, the jury,
the pre-training merge). A plain `git pull --rebase` merges those files line by line: two runs that changed the same
agent in different lines would leave it with one run's cash and the other's positions, and a real conflict lost the
round. Here each JSON file is merged per agent instead (3-way: the version this run started from, this run's, the
one now on GitHub): an agent only one side changed takes that side; one both changed takes the side with priority
(`--prioridade nossa` for the daily league, the jury and the training; `deles` for the rounds, whose next run redoes
the work in 10 minutes). Other files go through git as usual (on a clash, this run's version). The result must parse
and every agent must have its cash and positions, or nothing is pushed.

    uv run python scripts/save_state.py --mensagem "Agentes: stops" --prioridade deles [--caminhos config.json state.json lab]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"
MERGED = ("config.json", "state.json")


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", str(AGENTS_DIR), *args], check=check, capture_output=True, text=True).stdout


def show(rev: str, path: str) -> dict | None:
    out = subprocess.run(["git", "-C", str(AGENTS_DIR), "show", f"{rev}:{path}"], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def _merge_value(base, ours, theirs, ours_first: bool):
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    return ours if ours_first else theirs


def _as_map(items) -> dict | None:
    """config["agents"] is a list of dicts with an "id": as a map, to merge per agent (None if it is not)."""
    if isinstance(items, list) and all(isinstance(x, dict) and "id" in x for x in items):
        return {x["id"]: x for x in items}
    return None


def merge3(base: dict | None, ours: dict, theirs: dict | None, ours_first: bool) -> dict:
    """Per top-level key, and per agent inside "agents" (a dict of portfolios or a list of configs)."""
    if theirs is None:
        return ours
    base = base or {}
    out: dict = {}
    for key in list(dict.fromkeys([*theirs, *ours])):
        b, o, t = base.get(key), ours.get(key), theirs.get(key)
        if key == "agents" and o is not None and t is not None:
            if isinstance(o, dict) and isinstance(t, dict):
                bb = b if isinstance(b, dict) else {}
                merged = {}
                for aid in list(dict.fromkeys([*t, *o])):
                    if aid not in o and aid in bb:  # this run removed it
                        if t.get(aid) == bb.get(aid):
                            continue
                    if aid not in t and aid in bb:  # the other run removed it
                        if o.get(aid) == bb.get(aid):
                            continue
                    v = _merge_value(bb.get(aid), o.get(aid), t.get(aid), ours_first)
                    if v is not None:
                        merged[aid] = v
                out[key] = merged
                continue
            om, tm, bm = _as_map(o), _as_map(t), _as_map(b) or {}
            if om is not None and tm is not None:
                order = [x["id"] for x in (o if ours_first else t)] + [i for i in (tm if ours_first else om) if i not in
                                                                         {x["id"] for x in (o if ours_first else t)}]
                merged_list = []
                for aid in order:
                    if aid not in om and aid in bm and tm.get(aid) == bm.get(aid):
                        continue  # this run removed it
                    if aid not in tm and aid in bm and om.get(aid) == bm.get(aid):
                        continue  # the other run removed it
                    v = _merge_value(bm.get(aid), om.get(aid), tm.get(aid), ours_first)
                    if v is not None:
                        merged_list.append(v)
                out[key] = merged_list
                continue
        if key not in ours and key in base and t == b:
            continue  # this run removed it
        if key not in theirs and key in base and o == b:
            continue  # the other run removed it
        v = _merge_value(b, o, t, ours_first)
        if v is not None or key in ours or key in theirs:
            out[key] = v
    return out


def valid(path: str, data: dict) -> str | None:
    """Why this file must not be pushed, or None."""
    if not isinstance(data, dict):
        return f"{path}: não é um objeto"
    if path == "state.json":
        for aid, port in (data.get("agents") or {}).items():
            if not isinstance(port, dict) or not isinstance(port.get("cash", 0), int | float) \
                    or not isinstance(port.get("positions", {}), dict):
                return f"state.json: agente {aid} sem caixa ou posições válidas"
    if path == "config.json":
        ids = [c.get("id") for c in data.get("agents") or []]
        if len(ids) != len(set(ids)):
            return "config.json: agentes repetidos"
    return None


def save(message: str, ours_first: bool, paths: list[str], tries: int = 5) -> int:
    base_rev = git("rev-parse", "HEAD").strip()
    ours = {}
    for p in MERGED:
        f = AGENTS_DIR / p
        if f.exists():
            try:
                ours[p] = json.loads(f.read_text())
            except ValueError:
                print(f"::error::{p} desta execução não é JSON válido: nada guardado", flush=True)
                return 1
            if (why := valid(p, ours[p])):
                print(f"::error::{why}: nada guardado", flush=True)
                return 1
    git("add", "-A", "--", *paths, check=False)
    if subprocess.run(["git", "-C", str(AGENTS_DIR), "diff", "--cached", "--quiet"]).returncode == 0:
        print("Nada para guardar.", flush=True)
        return 0
    git("commit", "-qm", message)
    base = {p: show(base_rev, p) for p in ours}
    for attempt in range(tries):
        git("fetch", "-q", "origin", "main", check=False)
        remote = git("rev-parse", "origin/main").strip()
        if remote != base_rev:
            theirs = {p: show(remote, p) for p in ours}
            rebased = subprocess.run(["git", "-C", str(AGENTS_DIR), "rebase", "-q", "-X", "theirs", "origin/main"],
                                     capture_output=True, text=True)
            if rebased.returncode != 0:
                git("rebase", "--abort", check=False)
                print(f"::warning::Rebase falhou (tentativa {attempt + 1}); a tentar de novo", flush=True)
                time.sleep(5 * (attempt + 1))
                continue
            changed = False
            for p, mine in ours.items():
                merged = merge3(base[p], mine, theirs[p], ours_first)
                if (why := valid(p, merged)):
                    print(f"::error::{why} depois de juntar: nada guardado", flush=True)
                    return 1
                f = AGENTS_DIR / p
                indent = 2 if p == "config.json" else 1
                text = json.dumps(merged, indent=indent, ensure_ascii=False) + "\n"
                if f.read_text() != text:
                    f.write_text(text)
                    changed = True
                ours[p], base[p] = merged, theirs[p]
            if changed:
                git("add", *ours.keys())
                git("commit", "-q", "--amend", "--no-edit")
            base_rev = remote
        pushed = subprocess.run(["git", "-C", str(AGENTS_DIR), "push", "-q", "origin", "HEAD:main"],
                                capture_output=True, text=True)
        if pushed.returncode == 0:
            print("Guardado.", flush=True)
            return 0
        time.sleep(5 * (attempt + 1))
    print("::error::Não consegui guardar no repositório privado", flush=True)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mensagem", required=True)
    ap.add_argument("--prioridade", choices=["nossa", "deles"], default="nossa")
    ap.add_argument("--caminhos", nargs="*", default=["."])
    a = ap.parse_args()
    return save(a.mensagem, a.prioridade == "nossa", a.caminhos)


if __name__ == "__main__":
    sys.exit(main())
