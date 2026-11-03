# One Guillion Jollars: technical reference

The full reference for how every part works. For the overview, see the [README](../README.md).

## Claude Skills

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

`/acao TICKER`, `/etf TICKER`, `/mercado`, `/limites`, `/posicao capital
entrada stop [risco] [max_posicao]` and `/ajuda`. Discord calls a Cloudflare Worker
(`discord-bot/worker.js`), which answers `/posicao` and `/ajuda` itself and
starts `.github/workflows/discord-bot.yml` for the others; that workflow runs
`scripts/bot_command.py` and edits the reply with a card and chart (1-2 min).
The interaction token passed to the workflow is visible in the Actions run and
expires after 15 minutes.

`/limites` (`scripts/limits.py`) shows, per job (news, tips and rankings,
alerts, agents), how many times a day it runs and at what time (the Worker's
`/horario` schedule, read from KV), when it actually ran on the last day, and
what it compares (S&P 500 stocks and sectors, UCITS ETFs, VCP universe,
alert watchlist, stocks whose news is read; each run annotates its counts).
It also shows how the news reading and the stock data are doing: from the GitHub API, the last 7 days of news runs (headlines per
source, how many Claude read versus the FinBERT fallback, Claude failures, read
back from each run's annotations), the FMP calls of the last tips run (free
tier 250/day) and the Actions minutes this month (private repo, 2000 free);
plus one live test each of Claude (a tiny request on the subscription), the
headline search, FMP (one call), Yahoo Finance and the breadth CSVs. Claude Max
does not expose its remaining quota, so the live request is the check.

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
rankings 07:00 on weekdays, agents 07:30 (before the 08:00 European open), the
agents' intraday round 10:30/15:30/17:00/20:30, alerts 15:40/17:40/19:40 on weekdays, alert
follow-ups 22:30 and health check 23:00 on weekdays, weekly
summary Saturday 10:00. Server admins change them with `/horario`.

`scripts/weekly_summary.py` posts the week's index and sector changes, the
share of S&P 500 stocks above their 50-day average vs a week ago, the top
gainers and losers, and how the alerts of the last 5 weeks did
(`state/history/alerts.json`, kept in the Actions cache).

## Agents (paper trading league)

`scripts/agents.py` runs simulated agents with virtual euros on real prices
(`agents/config.json` defines them, `agents/state.json` holds portfolios,
trades, points and training history; the workflow commits both). This repo is
public, so `agents/` is not in it: it is a clone of the private repo
`one-guillion-jollars-dados`, which the workflows check out with the deploy key
`DADOS_DEPLOY_KEY` and push to. The pre-training's artifacts are encrypted with
`BACKUP_KEY` and its logs print no results. Locally:
`git clone https://github.com/tigogggg/one-guillion-jollars-dados agents`. Orders
decided on the latest close fill at the next session's open with 0.1% costs;
US stocks are bought in EUR at that day's EUR/USD rate, fractional shares
allowed. Stops and targets are checked against each session's low and high.

**Agents' holdings and position history.** Every live order carries the
numbers behind it (`decision_context`: trend score, 3-month return, distance to
the high, distance to the 50-day average, volume, technical score, zone, news,
stop; for sells the return and sessions held), copied into the trade. The
agent's card on the portal shows the current holdings with why each was
bought, and the position history (`position_history`: first buy to last sell)
as expandable entries with every buy and sell and its justification: the
strategy's own rule (`strategy_docs.justify`, sell reasons explained in
`SELL_REASONS`) plus those numbers. `agents.py portal` (workflow mode
`portal`) refreshes the portal's league page from the current state without
trading or posting.

**Interactive chart page.** The league and pre-training posts carry a
"📈 Ver gráfico interativo" button (and a clickable title). `scripts/interactive.py`
stores the chart's data in the Worker's KV under `chart:<random id>` for 120
days, and `discord-bot/chart_page.js` serves it at
`https://<worker>.workers.dev/g/<id>`: a Plotly chart (hover values, zoom,
agents on and off in the legend), countdowns to every scheduled run (from the
`/horario` schedule), an arcade horse race (one lane and avatar per agent,
advancing with its return; ▶ replays it day by day, with a live scoreboard),
and one card per agent. In the league page the card has
the holdings (quantity, average cost with fees, current price, value, gain,
weight, stop, target, earnings date), pending orders, every trade (price, value,
result, reason), realised gains per year, taxes, stats and the full strategy
description with its parameters; in the pre-training page, the backtest, exam,
crises, Monte Carlo and points per year instead of holdings. The static image
colours only the top 5 and the Indexador; the rest are faint grey. An older
page links to the newest one of its kind (`chart-latest:<kind>`).

**Profile (`#perfil`).** The member's name at the top right of the portal opens their profile: the display name ("Nome a apresentar", what other members see: 2-24 characters, no @ or links, not an agent's name, unique among members through KV `display-name:<key>`; empty goes back to the Discord name) and three tabs: 💼 Carteira (the portfolio), 👀 Seguir (the `/seguir` list) and 🔒 Conta e privacidade (sign-in accounts, sessions, version history, export, delete). Each tab is a frame of the portfolio page (`/c/<token>?embed=1&vista=seguir|conta`), loaded when first opened; the portfolio frame leaves those two out. The portfolio is no longer a tab; `/c/<token>` opened by its owner, and old `#carteira` links, land on the profile. The logo and the title go back to the main page (Hoje).

**Portal (`/portal`) and personal portfolios (`/carteira`).** `/portal` gives
the server's private portal link (`/p/<token>`, token in KV `portal:token`):
tabs for the race and chart, every agent's full card plus an overview table,
the news of the last 36 h with filters (`portal:news`, written by
`scripts/news.py`), buy and sell opportunities with a safety score
(`portal:market`, written by the agents' daily run with
`scripts/opportunities.py`), the schedule countdowns and how to use
`/carteira`. The safety score (0-100) adds eight published criteria: trend 25,
volatility 20, distance to the 52-week high 15, liquidity 10, company quality
15, results date 5, news 5, market zone 5 (rules in `opportunities.CRITERIA`
and on the page).

`discord_setup.py --portal` (workflow input `portal`) creates the read-only
`🏟️-portal` channel at the top of the 📈 FINANÇAS category and pins one
message there: the banner `discord/portal-banner.png` (drawn from
`discord/banner/portal-banner.html` with Chromium at 2x; Press Start 2P, SIL
OFL), where each summary arrives and when, and three buttons: open the portal
(link), "A minha carteira" (the Worker answers the member with their private
link, only to them) and "Como funciona" (the help). Running it again replaces
the message with a fresh one (new times, new link).

**Portal usability.** The portal opens on "🏠 Hoje": the market zone and
breadth gauge, the market context (VIX, policy uncertainty, credit spread,
dollar, S&P 500 trend), the 3 best proposals, the viewer's own portfolio
summary (the browser remembers the `/carteira` link once opened on that
device), the important news, the league podium and the next runs. Terms
underlined in dotted lines open a definition (also listed in "❓ Glossário").
The agents tab can compare two agents side by side, with both curves. On
phones the tabs move to a bottom bar. The portal and each portfolio can be
installed as an app (manifest at `.../manifest.webmanifest`, `/sw.js`, icons
in `discord-bot/icons.js` drawn from `discord/banner/app-icon.html`). The
portfolio has a "👀 Seguir" tab that edits the member's `/seguir` list.

**News analysis (`scripts/news_archive.py`, portal tab "📰 Notícias").** Each
news run keeps every rated headline for 30 days (`state/news/archive.json`, in
the news workflow's cache) and measures the price reaction 1 and 5 sessions
later, net of the S&P 500. The tab has: now (36 h, with filters), by sector,
what changed in 24 h (new important news, biggest score moves), per stock (30
days of headlines, a reaction chart, which stocks react to news), the calendar
(`publish_calendar` in the agents' run: macro events for 14 days, results
dates for 21 days of the ranked, held and proposed stocks) and accuracy (how
often the price moved the way the sentiment said, Claude vs FinBERT).

**Proposals (`scripts/proposals.py`, portal tab "🧠 Propostas").** After the
daily league, the orders the agents placed for the next open become proposals.
Each agent's vote is weighted by its record (`WEIGHT_RULES`: exam year of the
pre-training, its result in the current market zone, live excess return over
the S&P 500, Monte Carlo risk of ruin; Indexador and meta-agents do not vote).
Each buy gets a plan: maximum entry (close + 0.25 ATR), stop (2 ATR, at most
10%, or the agents' own if tighter), target at 2.5R (or the agents' own), the
net result at the target after costs and 28% tax, the supporting agents' trade
record (win rate, average win and loss, typical holding time: `trade_stats`
from the pre-training, or their live trades) and the safety score. The portal
turns it into a number of shares for the viewer's capital and risk (stored in
the browser). Every buy proposal is logged in `agents/proposals.json` and
followed: executed if the price reaches the entry within 3 sessions, then
stop, target or the close after 20 sessions; the scoreboard (hit rate, average
R, by confidence) is on the portal.

In `/carteira`, each open position gets a verdict (Manter / Reduzir / Vender,
rules on the page: trend against the 50- and 200-day averages, a 15% loss,
agents with 25%+ of the league's weight selling, bad news, concentration,
overextension) and a suggested stop; the "Propostas para ti" tab sizes the day's
proposals for the member's saved capital and risk and can fill the buy form.
Operations can be edited or duplicated (same operation, another date). The "📥 Importar" tab reads an XTB
account-history export (xlsx) in the browser with SheetJS: closed positions
become a buy and a sell, open positions a buy, short positions are left out,
XTB symbols are mapped to Yahoo's (`AAPL.US` → `AAPL`, `ASML.NL` → `ASML.AS`,
`BRK.B.US` → `BRK-B`), and the account's cash comes from the file's balance
(or the sum of its cash operations). Only the rows the member confirms are
sent; each keeps its XTB position number, so importing again adds nothing
twice. Each import belongs to an account (one per XTB currency account) whose
available cash shows in the totals and can be corrected by hand. The XTB
export is a zip with one xlsx per account (`<currency>_<account>_<from>_<to>`):
the page opens it with JSZip and imports every account at once, recognised by
its XTB number, so a later import goes to the same account whatever name is
typed. An import skips anything already in the portfolio (same XTB position,
or the same side, ticker, date, quantity and price from any account), the
operations tab offers to remove repeated operations, and an account can be
removed with or without its operations.

**Claude's portfolio analysis.** On the portfolio page, "🤖 Análise do Claude"
asks Claude (Sonnet, with the owner's subscription) for a structured analysis:
summary and score, diagnosis (diversification, ETF overlap, concentration,
currency, Portuguese taxes, idle cash), concrete moves (what, how much, why,
when, what would change the view), risks, scenarios and questions. The Worker
packs the holdings, cash per account, settings, verdicts, safety scores, news
and price reactions, calendar, market context and the agents' proposals into
KV (`analysis-req:<id>`) and starts `portfolio-analysis.yml` with only the id;
`scripts/portfolio_analysis.py` stores the answer under `analysis-res:<id>` and
the page polls for it. At most 3 analyses per member per 24 hours.

**Claude reviews the agents.** At the end of every pre-training (and by hand
with `agent-review.yml`), `scripts/agent_review.py` gives Claude each
strategy's results (20 years, exam, crises, Monte Carlo, what each parameter is
worth, the final phase, zone parameters) and its live record. Claude writes an
assessment per agent and up to 3 parameter sets per strategy, each with its
hypothesis (`agents/claude_review.json`, validated: known parameters only,
within bounds). The next pre-training adds them to the candidates on top of the
last chosen parameters; they are adopted only if they pass the same tests as
any other candidate. The report and the portal show each proposal's outcome.
Claude proposes; the backtest decides. The parameters chosen in the last three
pre-trainings are candidates too (exempt from the years rule, which they
passed before, but not from the plateau, crisis, exam and Monte Carlo tests), so
a retrain never drops a good set unless something beats it.

**Signing in.** Nothing inside the site (portal, charts, portfolios) opens
without a session: `/entrar` is the front door, and after signing in the member
goes back to the page they asked for. `/entrar` (`discord-bot/login.js`)
opens the portfolio with Discord (OAuth2 scopes `identify guilds`: only members
of the server; the first sign-in creates the portfolio) or with a Google
account the member linked on their portfolio page ("Contas para entrar").
A session cookie keeps the device signed in for 30 days; `/sair` ends it. The
Discord application and server ids are remembered from the interactions.
Secrets (GitHub → Worker on deploy, each login stays off until set):
`DISCORD_CLIENT_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`. Redirect
URIs: `https://<worker>/entrar/discord/callback` and `/entrar/google/callback`.

**Storage, backups and security.** With the D1 database `ogj` (found or
created by the deploy workflow; the Cloudflare token needs `D1 · Edit`), each
portfolio is a row with a version number (`discord-bot/store.js`): a save only
succeeds if nobody saved in between, so two tabs cannot overwrite each other,
and the last 30 versions stay for "Desfazer"/"Repor" in the portfolio's 🔒 Conta
tab. Portfolios still in KV move to D1 on first use; without D1 everything stays
in KV. Every night the Worker copies all portfolios and the members' KV keys
into D1's `backups` table (14 days), and `backup.yml` keeps an off-site copy as
an artifact (30 days, always encrypted with `BACKUP_KEY`; the job fails without it). D1 also shares the
Yahoo quotes (1 minute) and daily histories (6 hours) between visitors.
Every response carries security headers (CSP, no framing, `Referrer-Policy:
strict-origin` so the private path never leaks, HSTS) and rate limits per
address (Workers rate limiting: sign-in 20/min, portfolio changes 60/min, pages
300/min). A member can require sign-in (the link alone stops working), end the
sessions on every device, export the operations (CSV) or everything (JSON) and
delete the portfolio with everything linked to it.

**Portal extras.** A search box finds everything the portal knows about a
ticker (safety score, the agents' proposals and holdings, results date, news)
and the member's own position when their portfolio was opened on that device;
🩺 Estado shows when each part was last updated; a 3-step guide opens on the
first visit (📖 Guia reopens it). Scripts and styles are served once from
`/static/<name>-<hash>` and cached for a year, so pages are small.

**Opportunities.** Every stock lands in at most one list: 🔴 Vender (the
trend broke: below the 200-day average, below the 50-day with the 200-day
falling or 15% off the high, or 25% off the 52-week high), 🟡 Atenção (strong
but not now: results within 7 days, important negative news, lost the 50-day
in an intact uptrend, or more than 20% above it) and 🟢 Comprar (all of: Stage 2,
an entry signal (VCP, a fresh breakout on volume at most 5% above the pivot, or
a ranking leader back at its 50-day average), at most 12% above the 50-day, no
results within 10 days, no negative news, 100 M$/day, a stop within 8% and a
safety score of 70+, 80+ in a cautious market, none in a falling one; the best
10). The agents' buy proposals carry the same warnings and go to the end of the
list when they conflict.

**Economic factors.** The agents can react to (and the pre-training measures,
on 20 years of point-in-time data): the Geopolitical Risk index (Caldara &
Iacoviello, daily: wars and tensions in the news), Brent and gold over three
months, each sector's health (its SPDR ETF against the 200-day average and
against the S&P 500), exponential averages (21/50) and support levels (the last
confirmed swing low, for stops), and company fundamentals from the SEC's XBRL
filings (`scripts/fundamentals.py`, each number used only from its filing date):
quality (margins, revenue growth, free cash flow, current ratio, net debt) and
value (earnings and free-cash-flow yields, margin of safety against a simple
DCF). New shared parameters: `gpr_max`, `petroleo_max`, `ouro_max`,
`filtro_setor`, `ema_filtro`, `stop_suporte`, `qualidade_fund_min`,
`valor_fund_min`; and `evita_geo` live only (Claude reads the day's war,
sanctions, energy and macro headlines and rates the market risk 0-5, shown on
the portal). The 🧐 Analista buys solid, reasonably priced companies in an
uptrend, with trainable weights for quality, value and trend. Four more
strategies search other ways to invest: 🧮 Quant (momentum, trend, quality,
value and low volatility, each ranked within the index, with trainable
weights), 🧘 Monge (the calmest stocks above their 200-day average), 🦅 Abutre
(solid, cheap companies far below their high that start turning up) and ⚖️
Pêndulo (dual momentum: equity ETFs while they rise, bonds or gold otherwise).
Each pre-training run uses a new seed (`--semente`, the run number by default),
so consecutive runs explore different candidates while the best of the earlier
runs stays in the race. `context-data.yml` refreshes the data weekly.

**Training on your own computer.** The heavy search (many candidates, several
runs) costs about 1 000 GitHub Actions minutes per run; `scripts/train_local.py`
does the same on any machine with git, uv and internet, for free:
`uv run python scripts/train_local.py --execucoes 3 --paralelo 2 --push` downloads
the context once, trains each strategy in its own process (`--paralelo` at a time,
a few GB each), merges, applies and pushes `agents/` after each run.

**Platform updates.** The read-only 📢-novidades channel in 📈 FINANÇAS
(`discord_setup.py --canal-updates`). After each push that
changes the platform (`discord-bot/`, `scripts/`, workflows), `updates.yml`
waits five minutes so a burst of pushes becomes one post, then
`scripts/updates.py` gives Claude the commits since the last note (KV
`updates:last`) and posts what is new, what was fixed and what members need to
do. Bot data commits are ignored, and nothing is posted when only internal
things changed.

**Music.** Every page plays a YouTube track in a hidden player, muted by
default; the 🔇/🔊 button turns it on and the choice is remembered per device.

`/carteira` works only inside the server, so only its members get one: each
member gets one private link (`/c/<token>`, KV `portfolio-user:<discord id>`
and `portfolio:<token>`), `novo_link:True` replaces it.
`discord-bot/portfolio.js` stores their buys and sells (validated: the ticker
must exist on Yahoo Finance and a sale needs the shares on that date), values
the holdings with Yahoo Finance quotes every minute (average cost, gain per
position, today's change, realised gains, totals in euros at the current
rate) and draws value versus money invested since the first purchase.

Only the latest chart of each kind stays in `🤖-agentes`: after posting the
daily league, the monthly report or the pre-training report, the bot deletes
its own older posts with the same title in that channel (last 100 messages;
pinned posts are kept). See `discord_post.send(replace=...)`.

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

`.github/workflows/agents.yml` runs the daily league (07:30 Lisbon by default,
set with `/horario`) and answers `/agentes [nome]` and the admin-only `/agente
criar|depositar|remover`.

How the agents trade, live and in the pre-training alike:

- **Universe**: the S&P 500, the UCITS ETFs and ~330 large European companies
  (`data/universe/europe.csv`: symbol, name, GICS sector, country). A European
  stock is in play only while it is among the most traded 75% of that list
  (60-day traded value in euros), each day.
- **Decisions** use complete sessions only (`complete_bars`: a session still
  under way at its exchange is dropped, and every market is cut at the last full
  US session). The 07:30 run decides before any market opens; orders fill at
  that day's opens.
- **Currencies and costs**: prices are converted to euros with Yahoo's EURxxx=X
  rates (London quotes are in pence); the active broker's costs (`config.json` `corretora`: `xtb` by default,
  `trading212`, `ibkr`; see `agents.BROKERS`): spread per side, conversion outside the euro and, at Interactive
  Brokers, a commission per order (Europe 0.05% + fees, min 1.25 EUR, max 29 EUR; US 0.0035 USD a share, min
  0.35 USD), and buy-side taxes: France 0.4%, Italy 0.1%, Spain 0.2%, UK stamp
  duty 0.5%, Ireland 1% (foreign-registered companies and ETFs exempt).
- **Gaps**: a buy is cancelled when the stock opens more than `gap_max` (8% by
  default, tuned by the training) away from the close it was decided on.
- **Intraday round** (`agents.py stops`): fills the day's orders at the opens
  and executes stops and targets reached so far, at exactly the prices the daily
  run would use, so the portal shows them the same day. Reactive agents also
  decide again at that moment's prices; their new buys wait out the first 15 and
  the last 30 minutes of each exchange's session, and an agent down 3% since the
  last close makes no new buys until the next day (`NO_BUYS_AFTER_OPEN`,
  `NO_BUYS_BEFORE_CLOSE`, `DAY_LOSS_STOP`; never for exits, nor with
  `ignora_risco`).

## Congress trades

`scripts/congress.py` (with each news run) reads the US House Clerk's index of
Periodic Transaction Reports, the stock trades members must disclose under the
STOCK Act, and posts the new ones to the alerts channel: who, buy or sell,
stock, amount range, owner and how many days it took to disclose (up to 45 by
law, so the trades are days or weeks old). The PDFs are read with `pdftotext`;
scanned reports are linked. The reader is adapted from day-trader-skill (MIT).

## Earnings calendar

`scripts/earnings.py` looks up each stock's next earnings date on Yahoo
Finance, and keeps every past earnings release date from the SEC (8-K item
2.02, `data/earnings/`, weekly), so the backtest applies and trains the same
earnings rule (`dias_resultados`). The rankings mark stocks reporting within 10 days (📅), alert cards
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
- Learning: strategies that can be replayed are re-trained on the last 20
  years every Sunday (Worker job `treino`, 06:00 Lisbon, after the weekly
  market data; `pretrain.yml` with the last year as a sealed exam: reported,
  never used to choose). A new choice replaces the parameters an agent trades
  with only if the search wins out of sample (walk-forward, 6 years before the
  exam, against the original parameters) and its gain is not luck given the
  number of candidates (deflated Sharpe ratio ≥ 0.9).
  Random monthly mutation remains only for the live-only strategies
  (`LIVE_ONLY`).
- Catalisador: positive, important news confirmed by the price (up day on
  1.5x volume or a gap). Cético: important bad news the market shrugged off in
  an uptrend stock. Conselho: holds what at least 2 agents hold or are buying,
  without bad news. Maestro: copies the combined portfolio of the 3 agents with
  the best results over the last 20 sessions. Meta-agents decide after the
  others.

## Agent tuning (v4)

- **Shared tunable rules** (`agents.COMMON`, off by default, switched on and
  tuned by the training): `stop_atr` (initial stop at k x ATR), `trail_atr`
  (stop that rises to the close minus k x ATR), `tempo_max` (sell a losing
  position that did not move in N sessions), `min_tecnico` (minimum technical
  score 0-60: trend, volatility, distance to the high), `max_ext` (no buys
  more than x% above the 50-day average) and `peso_noticias` (news veto
  level). ATR is computed the same way live and in the backtest (parity test).
- **Parameters per market zone**: `params["por_zona"][zone]` overrides; the
  pre-training tries mutations for each zone with 10%+ of the training days and
  keeps one only if it adds 2% to the training points and does not lose on the
  exam.
- **Smarter pre-training**: two generations (the best 4 of the random draws
  get 2 mutations each), yearly points weighted for recency (0.85 per year
  back), at least 20 trades (except low-turnover strategies), a plateau test
  (nearby values must keep 70% of the score) before the crash test, exam and
  Monte Carlo gates.
- **Shadow copies**: every agent but the Indexador and the meta-agents runs 2
  paper copies with mutated parameters (`state["shadows"]`). After 20+
  sessions, a copy that made 2 percentage points more than its agent over the
  same sessions, and more points, becomes the agent's parameters (logged as
  "fantasma promovido"); a copy that does not within 60 sessions is replaced.
  The pre-training resets an agent's copies when it changes its parameters.

## Market context and parameter importance (v5)

`scripts/context.py` gives every day (live and over the 20-year backtest) the
VIX, the US Economic Policy Uncertainty index as a 3-year percentile (FRED
USEPUINDXD: the political-impact gauge), the high-yield credit spread (FRED
BAMLH0A0HYM2), the 3-month change of the 10-year yield, the 3-month change of
EUR/USD (the dollar seen from a euro investor) and the S&P 500 against its
200-day average. New shared, tunable rules use it: `vix_max`, `vix_meio`
(half size), `epu_max`, `credito_max`, `juros_max`, `usd_max` (US stocks
only), `filtro_spy`. Calendar rules without free history are tuned live only
(`LIVE_TUNED`: `dias_resultados`, the results-date window per agent;
`evita_macro`, no buys before Fed, inflation or jobs data; `peso_noticias`).
Live decisions record the context, so the justifications quote it.

Credit stress (`credit_dd`, used by `credito_max`) is measured from Yahoo
total-return prices, which always download: how far high-yield bonds have
fallen against US Treasuries from their 1-year best (Vanguard VWEHX / VFITX,
with HYG / IEF filling gaps). Policy uncertainty and the FRED high-yield spread
come from `data/context/`, refreshed monthly (and by hand) by the
`context-data.yml` workflow, which downloads FRED in small date ranges with
retries because full downloads time out from cloud machines; the latest days
are added live when FRED answers.

A final phase (`greedy_rules`) then switches on, one at a time, the shared
rules that help each strategy most: every rule that is off is tried at 25%,
50% and 75% of its range on the training years, the candidates are ranked by
gain, and each is kept (at most 4) only if it adds 2% to the training points,
does not lose on the exam year and does not fall deeper in the crises. The
zone parameters are tuned on top. The report and the portal list what it
switched on.

Every pre-training measures what each parameter is worth to each strategy
(`parameter_importance`): a parameter in use is reset to its default, a shared
rule that is off is switched on at mid-range, everything else equal, on the
training years and the exam year. `importance_summary` adds it up across
strategies for the report ("🔬 Que parâmetros contam") and the portal shows it
per agent.

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
- Costs of the active broker (`agents.BROKERS`, config `corretora`): XTB 0.1% spread + 0.5% conversion; Trading 212
  0.1% + 0.15%; Interactive Brokers 0.05% + 0.03% and a commission per order with a minimum. Each pre-training report
  also gives every strategy's verdict with each broker's costs (`corretoras`); runs with another broker are another
  scale (`pretrain.same_scale`), like another objective; 28% tax
  on each year's net realised gains, paid with the next year's IRS (31 August).
- Pre-training replays the S&P 500 as it was on each day (fja05680/sp500
  membership; delisted stocks only when Yahoo still has prices), uses the same
  `agents.decide()` as the live league, and accepts parameters only if they
  beat the originals in 65% of the training years and then on the exam year.

## Training v4: 20 years, crash tests, Monte Carlo, regime-aware Maestro

- `pretrain.py` replays 20 years by default (`--anos`), with the last year as
  the exam. UCITS ETFs that did not exist yet are extended backwards with US
  proxies (SPY, QQQ, sector SPDRs, …) converted to EUR (`ETF_PROXIES`).
- A candidate must beat the original parameters in 65% of the training
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

## Mirror server (`discord/mirror.json`)

`discord_setup.py --espelho <server id>` (workflow input `espelho`) creates the
📈 FINANÇAS category in a second server with `#billions` (open for chat) and
read-only copies of `📊-mercado`, `🚨-alertas-e-notícias`, `🤖-agentes` and
`🧪-testes`, checks the bot can post in each, and saves the ids to
`discord/mirror.json` on the branch it ran from. From then on every post that
goes through `discord_post.route()` is also sent to the channel of the same
kind there; a failed copy only warns. DMs, slash-command replies and the
👍/👎 vote reactions are not copied. The bot must first be added to that server
with an OAuth invite (Manage Channels and Manage Roles).

## Alert follow-ups

`scripts/alert_followups.py` (`alert-followups.yml`) is started by the Worker
on weekdays at 22:30 Lisbon, after the US close (`/horario acompanhamento`).
5 and 20 sessions after each opportunity alert it replies to the original
message with the change since the alert, the best point and whether the stop
was hit. Each milestone is posted once and marked in the alert history;
milestones more than 3 sessions late are skipped, so switching it on did not
post a backlog. A `testes` run posts to `🧪-testes` without marking anything.

## Bot health check (`🔧-estado`)

`scripts/health.py` (`health.yml`) is started by the Worker on weekdays at
23:00 Lisbon, after every other job (`/horario estado`). It checks whether
each scheduled job (tips, agents, alerts, news, alert follow-ups) ran on main
today and succeeded, plus how old the two breadth CSVs are (⚠️ after 2
weekdays), and posts one card to the read-only `🔧-estado` channel (created
with `discord_setup.py --canal-estado`, workflow input `canal_estado`). It
never posts anywhere else: without that channel it only prints the card in the
log, and the `testes` input sends it to `🧪-testes`.
