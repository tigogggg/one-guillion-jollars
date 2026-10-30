# Claude Skills

Claude Code skills for a structured trading process. The five skills in the
"No API Key Starter Path" were copied from
[tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)
(commit `c9b8419`, MIT, see `LICENSE-claude-trading-skills`) without changes.

| Skill | What it does | External data |
| --- | --- | --- |
| `market-breadth-analyzer` | Market breadth score 0-100 | Public CSV on tradermonty.github.io |
| `uptrend-analyzer` | Uptrend ratio / sector participation score 0-100 | Public CSV on raw.githubusercontent.com |
| `position-sizer` | Risk-based position sizing (fixed %, ATR, Kelly) | None |
| `trader-memory-core` | Thesis journal from idea to closed trade (local YAML) | Optional FMP API |
| `signal-postmortem` | Post-trade outcome review | Optional FMP API |

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
