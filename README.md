# Claude Skills

Claude Code skills for a structured trading process. The five skills in the
"No API Key Starter Path" plus `vcp-screener` were copied from
[tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)
(commit `c9b8419`, MIT, see `LICENSE-claude-trading-skills`) without changes.

| Skill | What it does | External data |
| --- | --- | --- |
| `market-breadth-analyzer` | Market breadth score 0-100 | Public CSV on tradermonty.github.io |
| `uptrend-analyzer` | Uptrend ratio / sector participation score 0-100 | Public CSV on raw.githubusercontent.com |
| `position-sizer` | Risk-based position sizing (fixed %, ATR, Kelly) | None |
| `trader-memory-core` | Thesis journal from idea to closed trade (local YAML) | Optional FMP API |
| `signal-postmortem` | Post-trade outcome review | Optional FMP API |
| `vcp-screener` | S&P 500 screen for Minervini VCP setups (pivot, stop, risk) | FMP API (one call per stock; free tier is 250/day) |

### Local changes to `vcp-screener`

The FMP free tier rejects multi-symbol quotes, so `screen_vcp.py` gained
`--history-quotes` (derive the pre-filter quote from each stock's history, one
call per stock), `--max-universe N` (largest N by market cap, or alternating
halves when FMP's company screener is unavailable) and `--api-budget N`.
`fmp_client.py` gained `get_market_caps()`. Defaults are unchanged.

## Daily Discord post

`scripts/discord_daily.py` runs the breadth skills (and `vcp-screener` when
`FMP_API_KEY` is set) and posts a Portuguese summary to `DISCORD_WEBHOOK_URL`.
`.github/workflows/discord-daily.yml` runs it on weekdays at 11:37 UTC; both
values are GitHub Actions secrets. Preview with `--dry-run`.

## Layout

The skills live in `skills/<name>/`, mirroring the upstream repo, because some
scripts (e.g. `trader-memory-core`) resolve the repo root from that path.
`.claude/skills/<name>` are symlinks so Claude Code discovers them when this
repo is opened.

## Setup

```bash
uv sync
```

Optional: `export FMP_API_KEY=...` for price lookups in `trader-memory-core`
and `signal-postmortem`.

To use the skills in every project, symlink or copy each `skills/<name>`
folder into `~/.claude/skills/`.

Tests: `uv run python -m pytest skills/<name>/scripts/tests`. The
`test_provenance_contract.py` files need the upstream repo's
`scripts/validate_provenance.py` and fail here; the skills themselves do not.

Not financial advice. These skills do not place orders.

## Rankings and opportunity alerts

`scripts/rankings.py` (Yahoo Finance via `yfinance`, no API key) posts, after
the daily tips, the top 3 S&P 500 stocks per sector by trend + quality and the
top 10 UCITS ETFs by trend. The watchlist it saves (plus the day's VCP setups)
is cached for `scripts/opportunity_alerts.py`, which
`.github/workflows/opportunity-alerts.yml` runs at 15:37, 17:37 and 19:37 UTC on
weekdays: it posts only when a watchlist stock breaks its pivot on projected
volume, once per stock per day. `yfinance` is an unofficial Yahoo client and
can break when Yahoo changes. Rankings are criteria-based, not buy advice.

Tests: `uv run python -m pytest tests`.

## Discord bot (slash commands)

`/acao TICKER`, `/etf TICKER`, `/mercado`, `/posicao capital entrada stop
[risco] [max_posicao]` and `/ajuda`. Discord calls a Cloudflare Worker
(`discord-bot/worker.js`), which answers `/posicao` and `/ajuda` itself and
starts `.github/workflows/discord-bot.yml` for the others; that workflow runs
`scripts/bot_command.py` and edits the reply with a card and chart (1-2 min).
The interaction token passed to the workflow is visible in the Actions run and
expires after 15 minutes.

Setup, once:

1. Discord Developer Portal → New Application. Copy the Application ID and
   Public Key; under Bot, reset and copy the token. Under Installation, add the
   `applications.commands` scope and install the app on the server.
2. GitHub → Settings → Developer settings → Fine-grained tokens: access to this
   repository only, permission Actions: Read and write.
3. Cloudflare → Workers & Pages → Create → Hello World worker named
   `captain-tips-bot`. Under Settings → Variables and Secrets add
   `DISCORD_PUBLIC_KEY`, `GITHUB_REPO` (`tigogggg/one-guillion-jollars`) and the
   secret `GITHUB_TOKEN`. The code, the `CONFIG` KV namespace and the cron
   trigger are deployed by `.github/workflows/deploy-worker.yml` on every change
   to `discord-bot/` (secrets `CLOUDFLARE_API_TOKEN`, from the "Edit Cloudflare
   Workers" template, and `CLOUDFLARE_ACCOUNT_ID`).
4. Discord app → General Information → Interactions Endpoint URL: the Worker's
   `https://….workers.dev` URL (Discord checks it on save).
5. Repository secrets `DISCORD_APPLICATION_ID` and `DISCORD_BOT_TOKEN`, then run
   the "Configurar o bot do Discord" workflow to register the commands and post
   the welcome card (pin it).

Messages are embeds with PNG charts (`scripts/charts.py`, `scripts/discord_post.py`);
`--dry-run` on any script saves the charts to `reports/preview/`.

## Schedule (`/horario`) and weekly summary

The Worker is also the scheduler. A Cloudflare cron trigger (`*/5 * * * *`)
calls its `scheduled()` handler, which dispatches `discord-daily.yml`,
`opportunity-alerts.yml` and `weekly-summary.yml` at the times stored in the
Worker's KV namespace (binding `CONFIG`), in Lisbon time. Defaults: tips and
rankings 11:40 on weekdays, alerts 15:40/17:40/19:40 on weekdays, weekly
summary Saturday 10:00. Server admins change them with `/horario`.

`scripts/weekly_summary.py` posts the week's index and sector changes, the
share of S&P 500 stocks above their 50-day average vs a week ago, the top
gainers and losers, and how the alerts of the last 5 weeks did
(`state/history/alerts.json`, kept in the Actions cache).

## Agents (paper trading league)

`scripts/agents.py` runs simulated agents with virtual euros on real prices
(`agents/config.json` defines them, `agents/state.json` holds portfolios,
trades, points and training history; the workflow commits both). Orders
decided on the latest close fill at the next session's open with 0.1% costs;
US stocks are bought in EUR at that day's EUR/USD rate, fractional shares
allowed. Stops and targets are checked against each session's low and high.

Points: +10 for each day in profit (equity above money given) and ±1 per 0.1%
of the day's change (capped at ±10). Training: on the first run of each month,
agents that lost money the previous month mutate their numeric parameters by
up to ±20% within bounds (`PARAM_BOUNDS`); winners keep theirs.

The ten starting agents (1000 € each): Indexador (S&P 500 buy & hold, the
benchmark), Guardião (VWCE sized by market zone), Rotador (top 3 UCITS ETFs),
Foguetão (top 5 trend scores), Joalheiro (top ranking with quality ≥ 70),
Caçador (breakouts on volume, 2% risk), Sniper (volatility contraction, 1%
risk), Campeão Setorial (best stocks of the strongest sectors), Pescador de
Quedas (pullbacks in uptrends) and Kamikaze (2 biggest 3-month gainers).

`.github/workflows/agents.yml` runs the daily league (12:15 Lisbon by default,
set with `/horario`) and answers `/agentes [nome]` and the admin-only `/agente
criar|depositar|remover`.

## Earnings calendar

`scripts/earnings.py` looks up each stock's next earnings date on Yahoo
Finance. The rankings mark stocks reporting within 10 days (📅), alert cards
turn orange with a warning when the stock reports within 5 days, `/acao` shows
the next date, the agents skip new buys of stocks reporting within 5 days
(except Kamikaze, `ignora_resultados`), and the weekly summary lists next
week's reports of the stocks in the rankings, recent alerts and agents'
holdings.

## Pre-training (`scripts/pretrain.py`)

A walk-forward backtest over the last 250 sessions with the live engine: on
each day the agents see only data up to that close and their orders fill at
the next open. Rolling-window features reproduce the live metrics exactly
(`tests/test_pretrain.py`). Each strategy tries ~24 random parameter sets
within `PARAM_BOUNDS`, scored by league points on the first ~68% of the
period; the best set is adopted only if it scores at least as well as the
original parameters on the remaining ~32% it never saw. Results go to
`agents/pretrain.json` and Discord; run "Pré-treino dos agentes" by hand.
Joalheiro is skipped (no historical fundamentals), the earnings rule is off and
today's S&P 500 list is used (survivorship bias).

## Leader signals, monthly report and macro agenda

The daily league shows the leading agent's orders for the next open, and
alert cards list the agents holding (or about to buy) the stock. On the first
run of a month the agents post a monthly report (return, points, best and
worst trade, vs the Indexador). `scripts/macro.py` adds high-impact US events
(Fed, CPI, jobs, GDP) to the daily tips (today/tomorrow) and the weekly
summary (next week), from FMP's economic calendar or, if unavailable, the
Fed's 2026 meeting dates and the usual first-Friday jobs report. The VCP
budget is 246 calls to leave room for it in the FMP free tier.

## Daily thread, votes and personal lists (bot user)

These need the app installed on the server with the `bot` scope (View
Channels, Send Messages, Send Messages in Threads, Create Public Threads, Read
Message History, Add Reactions, Embed Links, Attach Files) and use
`DISCORD_BOT_TOKEN` (`scripts/discord_bot.py`); without it everything still
posts, just without these extras.

- Daily thread: the tips message opens a thread; rankings and the agents'
  league post inside it (thread id shared via the watchlist cache). Alerts and
  the weekly summary stay in the channel.
- Votes: the bot adds 👍/👎 to each alert; the weekly summary counts people's
  votes per alert.
- `/seguir adicionar|remover|lista`: personal ticker lists (up to 20) kept in
  the Worker's KV; each alerts run DMs users whose stocks break out or move 5%+
  (once per stock per day), reading the lists through the Cloudflare API.

## News (`scripts/news.py`) and the Jornalista and Deriva agents

Three times per weekday (Worker job `noticias`, default 12:00, 16:00 and 20:30
Lisbon) `news.yml` collects recent Yahoo Finance headlines for the rankings,
the agents' holdings, the `/seguir` lists and the day's top S&P 500 movers,
and asks Claude (Claude Code CLI, `--model haiku`, authenticated with the
owner's subscription via the `CLAUDE_CODE_OAUTH_TOKEN` secret from
`claude setup-token`) to rate each one: sentiment -2..2, importance 0..3,
surprise and a Portuguese one-liner. If Claude fails, FinBERT
(`ProsusAI/finbert`, CPU) rates the tone instead. Important items are posted
to the day's thread; per-symbol scores (sentiment x importance, 36 h) go to
`state/news/signals.json` in the Actions cache.

- Jornalista buys stocks whose news score reaches `limiar` with importance
  ≥ 2 and sells on negative news; it cannot be pre-trained (no news history).
- Deriva (price only, pre-trained) buys stocks that gapped up ≥ 4% on 2x
  volume in the last days and held above the gap day's low.
- `/seguir` DMs include the stock's latest headline.

## Channels (`discord_setup.py --canais`)

Run "Configurar o bot do Discord" with `criar_canais` to create the
📈 FINANÇAS category with read-only channels (members can react and comment in
threads) — 📊-mercado (tips, rankings, weekly summary), 🚨-alertas-e-notícias
(alerts, news) and 🤖-agentes (league, monthly report, pre-training) — and to
move the webhook's channel into it for conversation and commands. The ids go to
`discord/channels.json`; `discord_post.route()` then posts each kind with the
bot user into its channel (needs Manage Channels and Manage Roles to create
them). Without that file everything posts to the webhook channel as before.

## Agent logic v2: news filter, monthly re-training, four new agents

- No agent buys a stock whose 36 h news score is at or below -3 (except
  Cético, whose strategy is exactly that, and agents with `ignora_noticias`).
- Learning: strategies that can be replayed are re-trained on the last 5
  years on the first weekday of each month (Worker job `treino`, 07:30
  Lisbon, `pretrain.yml` with the last year as exam). Random monthly mutation
  remains only for the live-only strategies (`LIVE_ONLY`).
- Catalisador: positive, important news confirmed by the price (up day on
  1.5x volume or a gap). Cético: important bad news the market shrugged off in
  an uptrend stock. Conselho: holds what at least 2 agents hold or are buying,
  without bad news. Maestro: copies the combined portfolio of the 3 agents with
  the best results over the last 20 sessions. Meta-agents decide after the
  others.

## Agent rules v3

- Reward: each day ±1 point per 0.1% the agent beat (or trailed) the S&P 500
  (SXR8), capped at ±10; +2 for a day in profit; minus 50 points per 100% of
  drawdown beyond 10% from the agent's best level.
- Bankruptcy: an agent below 50% of the money it was given is liquidated and
  retired; a child of the current leader (same strategy, mutated parameters,
  1000 EUR, next generation) takes its place.
- Risk (all agents without `ignora_risco`): buy size scaled down for stocks
  with daily volatility above 2% (to half at 4%), at most 40% of equity per
  sector, no new buys while 20% or more below the best level.
- Costs: 0.1% per side, plus 0.15% currency conversion for US stocks; 28% tax
  on each year's net realised gains, paid on the first run of the next year.
- Pre-training replays the S&P 500 as it was on each day (fja05680/sp500
  membership; delisted stocks only when Yahoo still has prices), uses the same
  `agents.decide()` as the live league, and accepts parameters only if they
  beat the originals in 3 of every 4 training years and then on the exam year.

## Training v4: 20 years, crash tests, Monte Carlo, regime-aware Maestro

- `pretrain.py` replays 20 years by default (`--anos`), with the last year as
  the exam. UCITS ETFs that did not exist yet are extended backwards with US
  proxies (SPY, QQQ, sector SPDRs, …) converted to EUR (`ETF_PROXIES`).
- A candidate must beat the original parameters in 3 of every 4 training
  years, must not go bankrupt or fall more than 5 points deeper than the
  original in the 2008, 2011, 2020 and 2022 crises, must hold up on the exam,
  and its Monte Carlo risk of ruin (1000 alternative years from one-month
  blocks of its daily returns) must not be clearly higher.
- `pretrain.yml` trains each strategy on its own runner in parallel
  (`--saida`) and a final job merges, applies and reports (`--juntar`).
- Results per market zone (`by_zone`) feed the Maestro, which now copies the
  agents that best beat the S&P 500 in the current zone (live record once it
  has 15 days there, pre-training until then).
- The 2000 crash is not covered (it would need data from 1999 and more memory),
  and most companies that failed in 2008 have no prices on Yahoo, so crisis
  results remain somewhat optimistic; the report shows the coverage.

## Readiness panel (`/prontidao`)

Posted with each monthly report and on demand: for every agent, ✅/❌ on
1) 126 live sessions, 2) beating the Indexador's live return with an equal or
better Sharpe, 3) a live return no worse than the backtest's Monte Carlo
worst-1-in-20 year (prorated), 4) 30 closed trades (not for low-turnover
agents) and 5) a live max drawdown of 25% or less. Agents passing all checks
are listed as ready for phase 2: small real-money ideas, human decisions.

## Inflation and deposit lines on the return charts

`scripts/references.py` adds two grey reference lines to the league and
pre-training charts once they span 180 days or more: **Inflação** (euro area
HICP from Eurostat's `prc_hicp_minr`, held flat until the next month is
published) and **Depósito** (money at the ECB deposit facility rate, compounded
daily and floored at 0%). An agent below Depósito did not earn its risk; one
below Inflação lost purchasing power. The lines are not scored, and a chart
is drawn without them when either source is unreachable.

## Test channel (`🧪-testes`)

`discord_setup.py --canal-testes` (workflow input `canal_testes`) creates a
read-only `🧪-testes` channel in the 📈 FINANÇAS category and saves its id to
`discord/channels.json` on the branch the workflow ran from. With
`DISCORD_TESTES` set, `discord_post.route()` sends every post there, and fails
instead of posting to the real channel when it is missing. The pre-training
workflow's `testes` input does this for the report-only run, so a branch can be
tried out with `gh workflow run pretrain.yml --ref <branch> -f so_relatorio=true -f testes=true`.
