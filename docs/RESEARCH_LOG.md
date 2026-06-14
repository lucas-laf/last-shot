# Research Log — edge-hunt toward mid-4-figures/month

Running state for the [GOAL.md](GOAL.md) research loop. Each cycle: read this,
do one unit of work, append results, update the ranking + next action.
Newest entries at the top of the Journal.

## Best-candidate ranking (live)

| # | candidate | status | est. $/mo @ $25k | confidence | evidence |
|---|-----------|--------|------------------|------------|----------|
| — | _none validated yet_ | — | — | — | — |

## Hypothesis backlog

| id | hypothesis | track | status | note |
|----|-----------|-------|--------|------|
| H1 | Soft-book / exchange odds are systematically beatable vs Pinnacle's de-vigged line (value betting) | sports | open | core +EV test; backtest with results |
| H2 | Cross-venue **arbs** (best back across books vs exchange lay; inverse-odds sum < 1) exist at deployable size/frequency | sports | open | measure freq × size = deployable edge/day |
| H3 | Polymarket lags the sharp (Pinnacle) line on shared events — directional convergence (NB: prior convergence test failed OOS) | sports/PM | open | needs a sharper signal than before |
| H4 | Polymarket LP / liquidity rewards = positive net yield after hedging inventory | PM | open | market-neutral; read reward params per market |
| H5 | Crypto spot (Coinbase) time-series / cross-sectional momentum baseline is +EV after costs | crypto | open | tertiary; free data |

## Open questions / blockers

- Operator: token/time budget + cadence for the loop.
- OddsPapi quota/rate limits — TBD (watch headers).
- Live execution needs Pinnacle + an exchange/soft book account (gated, later).

## Method reminders

- **Measure deployable edge/day**, not % return (recycle model).
- Kill fast; a hypothesis needs to clear costs (commission/vig/spread) AND have
  real depth/capacity, not just exist on paper.
- Phantom liquidity + market impact + bookmaker limits/gubbing are the usual
  edge-killers — account for them before believing a number.
- No real money without operator sign-off.

## Journal

### 2026-06-14 — setup
- Goal scoped: $5k/mo on $10–50k, directional/quant allowed, UK access
  (Betfair/PM/Coinbase-spot; will open Pinnacle + exchanges/soft books).
- Data secured: **OddsPapi** (350+ books incl. Pinnacle + Betfair, live+historical),
  **API-Football** (Pro, 7,500/day), football-data.co.uk free CSVs.
- Confirmed OddsPapi key live (HTTP 200, /v4/sports). API-Football key live (Pro).
- Next action: **H1/H2 first measurement** — pull a live odds snapshot for a major
  soccer league across Pinnacle + soft books + Betfair, compute (a) value-bet edge
  vs Pinnacle de-vigged line, (b) arb frequency/size → first deployable-edge read.
