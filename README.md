# last_shot — Polymarket ↔ Betfair price-discrepancy scanner

Discovers markets on both platforms, matches equivalent ones with an LLM,
tracks matched pairs in real time, and **paper-trades** divergences that clear
fees. No real orders are placed anywhere.

## Setup

```bash
uv venv --python 3.12 && uv sync --extra dev
```

Secrets go in `.env` (already present): Betfair app key + username/password
(interactive login; a session token or cert files also work), plus
`ANTHROPIC_API_KEY` for the LLM matching stage. `BETFAIR_COMMISSION` and
`MIN_EDGE` override `config.yaml`.

## Pipeline

```bash
# 1. Scan both platforms (run hourly-ish; incremental upserts)
.venv/bin/python -m src.discovery.run

# 2. Match markets — Claude (orchestrating in-session) is the reviewer:
.venv/bin/python -m src.matching.export            # dump unjudged pairs to data/candidates.json
#    ... Claude reads the export, judges each pair, writes data/verdicts.json ...
.venv/bin/python -m src.matching.ingest data/verdicts.json
# (src.matching.run still exists for API-based judging if ANTHROPIC_API_KEY is set)

# 3. List what's tracked / manually re-review
.venv/bin/python -m src.matching.review --list
.venv/bin/python -m src.matching.review            # interactive approve/reject

# 4. Track matched pairs live (Betfair Stream API + Polymarket WS),
#    record ticks to data/ticks/*.parquet, log paper trades on signals
.venv/bin/python -m src.tracking.run

# 5. Settle paper trades after events resolve
.venv/bin/python -m src.signals.settle

# 6. The go/no-go report: realized edge after fees, by category & signal type
.venv/bin/python -m analysis.edge_report
```

Tests: `.venv/bin/python -m pytest -q`

## How signals work

All prices are normalized to implied probability with a bid/ask band
(`src/tracking/normalize.py`). Effective prices include fees
(`src/signals/fees.py`): Betfair commission on net winnings, Polymarket
taker fee `rate × min(p, 1−p)` (3% sports / 4% politics schedules, read
per-market from the API). Two signal types (`src/signals/engine.py`):

- **lock_arb** — buy one platform, sell the other; profit locked at
  settlement regardless of outcome. Both legs are logged.
- **convergence** — executable price deviates from the deeper book's mid
  by more than fees + `MIN_EDGE`. Inventory risk; logged to measure
  whether it actually pays.

Signals only fire when both feeds are fresh (<30s) and respect a per-pair
cooldown. Paper trades snapshot the top-5 books on both platforms at signal
time and cap stake at displayed depth, so fill realism can be audited later.

## Known constraints

- Betfair app key is **live** (detected at login) → Stream API, real-time.
- LLM matching compares resolution rules text and flags `resolution_risk`;
  risky or low-confidence matches go to the review queue rather than
  auto-tracking. Hallucinated runner mappings are rejected mechanically.
- GBP↔USDC FX is ignored in paper PnL (flagged in the report).
- Betfair Premium Charge (20–60% on gross profits) applies only if
  consistently profitable with real money — relevant to go/no-go, not to
  paper trading.
- Soccer discovery caps at 200 markets/event type (catalogue weight limit),
  sorted by traded volume so the liquid ones come first.
