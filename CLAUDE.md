# One Guillion Jollars: notes for Claude Code

Discord trading community "One Guillion Jollars" (owner: Tiago, GitHub `tigogggg`, Portuguese from Portugal).
Reply to him in **pt-PT**, short and clear; commit messages in English.

## What is here

- `discord-bot/`: Cloudflare Worker (site + Discord interactions). Portal (`chart_page.js`), personal portfolios
  (`portfolio.js`), sign-in with Discord/Google (`login.js`, `session.js`), storage D1 + KV (`store.js`).
  The whole site requires sign-in; `/` is the portal. Deployed by `.github/workflows/deploy-worker.yml` on push.
- `scripts/`: Python (run with `uv run python scripts/...`). Agents league (`agents.py`), pre-training / backtest
  (`pretrain.py`), market context (`context.py`: VIX, policy uncertainty, credit, rates, dollar, geopolitical risk,
  oil, gold, sector ETFs), SEC fundamentals point-in-time (`fundamentals.py`), opportunities (`opportunities.py`),
  proposals (`proposals.py`), news + Claude geopolitics (`news.py`), Claude agent review (`agent_review.py`),
  platform update notes (`updates.py`), evolution history (`evolution.py`), local training (`train_local.py`).
- Tests: `uv run pytest -q tests` (must pass before pushing).
- Data in git: `data/context/*.csv`, `data/fundamentals/fundamentals.csv.gz` (refreshed weekly by `context-data.yml`).
- Agents state: `agents/config.json`, `agents/state.json`, `agents/pretrain.json` (latest pre-training),
  `agents/pretrain_history.json` (one entry per run), `agents/search.json` (search intensity: 2× both locally and in Actions, where each strategy has its own 4-core runner).
  `agents/` is NOT in this public repo: it is a clone of the private repo `tigogggg/one-guillion-jollars-dados`
  (gitignored here; commit and push inside `agents/`). Workflows check it out with the deploy key `DADOS_DEPLOY_KEY`.
  Pre-training artifacts are encrypted with `BACKUP_KEY`; Actions logs are public, so never print results there.

## Rules

- Never ask for secrets; never commit personal files (XTB exports), tokens or `.env`.
- Commits as `tigogggg <tiago.gmrs@gmail.com>`; always `git pull --rebase origin main` before `git push`
  (GitHub Actions bots push to main too).
- The repo is public: no secrets in code; GitHub secrets hold tokens (Discord, Cloudflare, Claude OAuth, SEC user agent,
  Google/Discord OAuth).
- Results must not be faked: report what tests/runs actually show.
- Study results and figures (pre-training scores, returns, notes) go only to `agents/` (private repo): never in
  this repo's files, commit messages or Actions logs.

## Pre-training (finding the best parameters for each agent)

Run on this PC (free, uses all cores the memory allows):

    git pull && git -C agents pull
    uv run python scripts/train_local.py --ate-convergir --push

It repeats runs until no strategy improves for 2 runs (max 12), merges, applies and pushes `agents/`.
`--perfil conservador|equilibrado|arrojado|todos` picks the investor profile (default equilibrado). Each profile has
its own agents (`<id>-conservador`, `<id>-arrojado`) and report (`pretrain.json`, `pretrain-<perfil>.json`). Points are
the profile's grade (`agents.profile_score`) minus the S&P 500's over the same days (10 points ≈ 1%/year).
A run only replaces a strategy's parameters if the exam (last year, out of sample) does not get worse and
training + exam together improve by at least 5% and 5 points (`CHANGE_MARGIN`, `CHANGE_MIN`; weekly runs on Sundays must not swap for noise); the last 3 choices always stay candidates.
In Actions the profiles run one after another (input `seguintes`, default: conservador and arrojado after equilibrado). If a merge failed:
`--retomar out/local/<run folder>`. Logs: `out/local/<run>/<strategy>.log`.

After a run is pushed, the portal shows it once GitHub Actions republishes: dispatch
`pretrain.yml` with `so_relatorio=true` (report + evolution chart) and `agents.yml` with `mode=portal`.

## Current state

Results, run figures and the study's working notes are in the private repo: `agents/NOTES.md`. Read it there;
add new state and results there, never here.
