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
