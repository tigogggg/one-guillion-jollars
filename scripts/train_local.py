"""Train the agents on your own computer (no GitHub Actions minutes), several runs in a row, then push.

The heavy search for each strategy's best parameters (many candidates, several runs with different seeds)
only has to be done once; the weekly re-training in GitHub Actions (Sundays) then refines it. This script does what
pretrain.yml does, on this machine:

1. downloads the market context once (the same for every strategy);
2. trains each strategy in its own process, PARALELO at a time (each 20-year run needs a few GB of memory);
3. merges the results, applies the parameters to the agents (agents/pretrain.json and agents/state.json);
4. repeats for RUNS runs, each with a new seed: the best of the earlier runs always stays a candidate and a run
   only replaces a strategy's parameters with better ones (training and exam together);
5. with --push, commits and pushes agents/ so the live agents use the new parameters. agents/ is a clone of the
   private repo one-guillion-jollars-dados (this repo is public), so that is where the push goes.

    uv run python scripts/train_local.py --execucoes 3 --paralelo 2 --push
    uv run python scripts/train_local.py --ate-convergir --paralelo 2 --push   # until no strategy improves
    uv run python scripts/train_local.py --ate-convergir --perfil todos --push  # each investor profile in turn

Needs: git, uv and internet (Yahoo Finance, FRED, the Geopolitical Risk index). Discord posting is skipped
unless DISCORD_WEBHOOK_URL / DISCORD_BOT_TOKEN are set in the environment.
To merge an Actions run whose last step did not run, dispatch pretrain.yml with juntar_de=<run id> (its artifacts
are encrypted with BACKUP_KEY, which only Actions and you have).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = [sys.executable]
STRATEGIES = ["guardiao", "rotador", "foguetao", "analista", "quant", "monge", "abutre", "pendulo", "cacador", "sniper",
              "campeao", "pescador", "deriva", "kamikaze", "estratega", "navegador", "tematico", "indexador"]


AGENTS = REPO / "agents"  # clone of the private data repo
PROFILES = ["equilibrado", "conservador", "arrojado"]  # agents.PROFILES; the balanced one first


def report_path() -> Path:
    """The report of the profile being trained (PRETRAIN_PERFIL, as scripts/pretrain.py reads it)."""
    profile = os.environ.get("PRETRAIN_PERFIL") or "equilibrado"
    return AGENTS / ("pretrain.json" if profile == "equilibrado" else f"pretrain-{profile}.json")


def run(cmd: list[str], log: Path | None = None, cwd: Path = REPO) -> int:
    print("$", " ".join(cmd), flush=True)
    if log:
        with log.open("w", encoding="utf-8") as f:
            return subprocess.call(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT)
    return subprocess.call(cmd, cwd=cwd)


def memory_gb() -> float | None:
    """Free physical memory in GB (Windows, Linux, macOS), or None."""
    try:
        if sys.platform == "win32":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = Status()
            st.dwLength = ctypes.sizeof(Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullAvailPhys / 1e9
        if hasattr(os, "sysconf"):
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9
    except Exception:  # noqa: BLE001
        return None
    return None


def parallelism(requested: int, n: int) -> int:
    """Strategies at once: the request, or (0 = auto) as many as free memory and cores allow (agents/search.json)."""
    try:
        cfg = json.loads((REPO / "agents" / "search.json").read_text()).get("local", {})
    except (OSError, ValueError):
        cfg = {}
    if requested > 0:
        return min(requested, n)
    if cfg.get("parallel"):
        return min(int(cfg["parallel"]), n)
    cores = max(1, (os.cpu_count() or 2) - 1)
    mem = memory_gb()
    by_mem = int(mem // float(cfg.get("gb_per_strategy", 5))) if mem else 2
    return max(1, min(n, cores, by_mem))


def train_all(strategies: list[str], years: int, parallel: int, seed: str, ctx: Path, out: Path) -> list[str]:
    """Each strategy in its own process, `parallel` at a time; returns the ones that failed."""
    out.mkdir(parents=True, exist_ok=True)
    pending, running, failed = list(strategies), {}, []
    while pending or running:
        while pending and len(running) < parallel:
            s = pending.pop(0)
            log = out / f"{s}.log"
            cmd = PY + ["scripts/pretrain.py", "--estrategias", s, "--anos", str(years), "--contexto", str(ctx),
                        "--saida", str(out / f"{s}.json"), "--semente", seed]
            print(f"  ▶ {s} (registo em {log})", flush=True)
            running[s] = (subprocess.Popen(cmd, cwd=REPO, stdout=log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT),
                          time.time())
        time.sleep(10)
        for s, (proc, start) in list(running.items()):
            if proc.poll() is None:
                continue
            minutes = (time.time() - start) / 60
            if proc.returncode == 0 and (out / f"{s}.json").exists():
                print(f"  ✔ {s} em {minutes:.0f} min", flush=True)
            else:
                print(f"  ✘ {s} falhou ({proc.returncode}); ver {out / f'{s}.log'}", flush=True)
                failed.append(s)
            del running[s]
    return failed


def chosen_params() -> dict:
    """{strategy: chosen parameters} from the saved pre-training (to see what a run changed)."""
    path = report_path()
    if not path.exists():
        return {}
    return {s: r.get("chosen") for s, r in json.loads(path.read_text()).get("strategies", {}).items() if "year" in r}


def push(a, i: int) -> None:
    if not a.push:
        return
    run(["git", "add", "-A"], cwd=AGENTS)
    if subprocess.call(["git", "diff", "--cached", "--quiet"], cwd=AGENTS) != 0:
        run(["git", "commit", "-m", f"Agentes: pré-treino local {datetime.now():%Y-%m-%d} (execução {i})"], cwd=AGENTS)
        run(["git", "pull", "--rebase", "origin", "main"], cwd=AGENTS)
        run(["git", "push", "origin", "HEAD:main"], cwd=AGENTS)


def summary() -> None:
    report = json.loads(report_path().read_text())
    rows = [(s, r) for s, r in report["strategies"].items() if "year" in r]
    for s, r in sorted(rows, key=lambda kv: -kv[1]["year"]["return"]):
        y, v = r["year"], r.get("valid", {})
        kept = " (manteve a escolha anterior)" if r.get("kept_previous") else ""
        print(f"  {s:10} 20 anos {y['return'] * 100:+7.0f}%  queda máx. {y['max_drawdown'] * 100:4.0f}%  "
              f"exame {v.get('best', {}).get('points', 0):5} vs {v.get('default', {}).get('points', 0):5}{kept}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles are not UTF-8 by default
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ["PYTHONUTF8"] = "1"  # Windows reads files as cp1252 by default; the repo's files are UTF-8
    os.environ.setdefault("PYTHONWARNINGS", "ignore")  # yfinance's pandas deprecation warnings flood the terminal
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execucoes", type=int, default=3, help="Runs in a row, each with a new seed (default 3)")
    parser.add_argument("--ate-convergir", action="store_true",
                        help="Keep running until no strategy finds better parameters in PACIENCIA runs in a row (at most --execucoes)")
    parser.add_argument("--paciencia", type=int, default=2, help="Runs without any improvement before stopping (default 2)")
    parser.add_argument("--paralelo", type=int, default=0, help="Strategies trained at the same time (default 0 = auto: by free memory and cores)")
    parser.add_argument("--anos", type=int, default=20)
    parser.add_argument("--estrategias", default=",".join(STRATEGIES), help="Comma-separated (default: all trainable)")
    parser.add_argument("--push", action="store_true", help="Commit and push agents/ after each run")
    parser.add_argument("--revisao", action="store_true", help="Ask Claude for proposals between runs (needs the claude CLI)")
    parser.add_argument("--perfil", default="equilibrado", choices=PROFILES + ["todos"],
                        help="Investor profile to train for (default equilibrado; todos = each one in turn)")
    parser.add_argument("--retomar", help="First merge and apply an earlier run's folder (e.g. out/local/run1-...) whose merge failed")
    a = parser.parse_args()
    strategies = [s.strip() for s in a.estrategias.split(",") if s.strip()]
    if "indexador" not in strategies:
        strategies.append("indexador")  # the benchmark the others are scored against
    work = REPO / "out" / "local"
    # prices downloaded once a day and shared by the strategies trained in parallel
    os.environ.setdefault("PRICE_CACHE_DIR", str(work / "precos" / datetime.now().strftime("%Y-%m-%d")))
    ctx = work / "context.csv"
    work.mkdir(parents=True, exist_ok=True)
    print("Contexto de mercado (uma vez)...", flush=True)
    if run(PY + ["scripts/context.py", "--guardar", str(ctx)]) != 0:
        return 1
    dry = [] if os.environ.get("DISCORD_WEBHOOK_URL") or os.environ.get("DISCORD_BOT_TOKEN") else ["--dry-run"]
    if a.ate_convergir and a.execucoes == 3:
        a.execucoes = 12  # a ceiling; it usually stops much earlier
    for profile in (PROFILES if a.perfil == "todos" else [a.perfil]):
        os.environ["PRETRAIN_PERFIL"] = profile  # read by every scripts/pretrain.py started from here
        print(f"\n##### Perfil {profile} #####", flush=True)
        if series(a, strategies, work, ctx, dry) != 0:
            return 1
        a.retomar = None  # only before the first profile
    print("\nFeito." + ("" if a.push else " Para os agentes ao vivo usarem os parâmetros: cd agents && git add -A && git commit -m pre-treino && git push"))
    return 0


def series(a, strategies: list[str], work: Path, ctx: Path, dry: list[str]) -> int:
    """One profile's runs (until it converges with --ate-convergir)."""
    if a.retomar:
        print(f"Juntar a execução anterior em {a.retomar}...", flush=True)
        if run(PY + ["scripts/pretrain.py", "--juntar", a.retomar, "--apply"] + dry) != 0:
            return 1
        summary()
        push(a, 0)
    quiet = 0
    for i in range(1, a.execucoes + 1):
        if a.push and i > 1:  # picks up improvements to the training published meanwhile (scripts, data, agents)
            run(["git", "pull", "--rebase", "origin", "main"])
            run(["git", "pull", "--rebase", "origin", "main"], cwd=AGENTS)
        before = chosen_params()
        seed = datetime.now().strftime("%Y%m%d%H%M%S")
        out = work / f"run{i}-{seed}"
        print(f"\n=== Execução {i} de {a.execucoes} (semente {seed}) ===", flush=True)
        width = parallelism(a.paralelo, len(strategies))
        print(f"{width} estratégias em paralelo ({os.cpu_count()} núcleos, {memory_gb() or 0:.0f} GB livres)", flush=True)
        failed = train_all(strategies, a.anos, width, seed, ctx, out)
        if failed:
            print(f"Falharam: {failed} (as restantes são juntas na mesma)", flush=True)
        if run(PY + ["scripts/pretrain.py", "--juntar", str(out), "--apply"] + dry, out / "juntar.log") != 0:
            print(f"Não consegui juntar; ver {out / 'juntar.log'}", flush=True)
            return 1
        summary()
        after = chosen_params()
        changed = sorted(k for k in after if after.get(k) != before.get(k))
        print(f"Estratégias com parâmetros melhores nesta execução: {', '.join(changed) or 'nenhuma'}", flush=True)
        quiet = 0 if changed else quiet + 1
        if a.ate_convergir and quiet >= a.paciencia:
            print(f"Convergiu: {a.paciencia} execuções seguidas sem melhorias.", flush=True)
            push(a, i)
            break
        if a.revisao:
            run(PY + ["scripts/agent_review.py"] + (["--dry-run"] if dry else []))
        push(a, i)
    return 0


if __name__ == "__main__":
    sys.exit(main())
