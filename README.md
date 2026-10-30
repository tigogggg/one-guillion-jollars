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
3. Cloudflare → Workers & Pages → Create → Hello World worker, replace its
   code with `discord-bot/worker.js`, deploy. Under Settings → Variables and
   Secrets add `DISCORD_PUBLIC_KEY`, `GITHUB_REPO` (`tigogggg/one-guillion-jollars`)
   and the secret `GITHUB_TOKEN`.
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
