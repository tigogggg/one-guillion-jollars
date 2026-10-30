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
