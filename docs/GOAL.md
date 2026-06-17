# Research Goal — find an edge that can reach mid-4-figures/month

> ⚠️ **HISTORICAL — SUPERSEDED.** This was the broad edge-hunt goal. The hunt is
> concluded: the sharp-line hypotheses (H1/H2/H3/H5) were killed; the only surviving
> approach is **Polymarket LP rewards — see [LP_OVERVIEW.md](LP_OVERVIEW.md)**. Kept for
> context only; not the current direction.

_Status: scoped 2026-06-14 (capital, risk, access, data all set). Pending: budget/
cadence + operator "go". Progress tracked in [RESEARCH_LOG.md](RESEARCH_LOG.md)._

## Objective

Find and validate one or more **systematic trading edges** whose **combined
expected value is ≥ $5,000/month** at the stated capital — and keep iterating
until at least one candidate clears the bar with reasonable confidence, or it's
documented that no such edge is reachable and why.

## Parameters

- **Capital envelope:** $10–50k deployable (plan against **~$25k**). $5k/mo ≈
  **10–50%/month** — aggressive; expect a high hypothesis kill-rate.
- **Risk appetite:** directional / quant **allowed** (not limited to riskless arb).
  Market-neutral, market-making, stat-arb, and directional signal strategies are
  all in scope, with stated drawdown.
- **Success bar (per candidate):** a documented **backtest and/or paper-trading
  record** with: expected $/month at the capital level, Sharpe or hit-rate, max
  drawdown, capacity (how much capital before the edge decays), and a confidence
  level. The deliverable is a **ranked shortlist with evidence**, not one
  guaranteed winner.
- **Checkpoint cadence:** report at each milestone (hypothesis screened /
  backtested / paper-validated). **No real money moves without explicit sign-off.**
- **Budget:** [token/time budget per run, or "run to next milestone"] — _to set._
- **Spend authority:** free/cheap data + paper accounts OK without asking; real
  funding and live trading are gated.

## Access constraints (operator, 2026-06-14)

**Have:** Betfair, Polymarket, Coinbase **Kalshi: no access** (US
only). **Will open:** other betting exchanges (Smarkets/Matchbook) + soft
sportsbooks incl. **Pinnacle** (sharp, high-limit, no gubbing — the key reference).

## Capital model (the lens)

monthly P&L = **(deployable capital per cycle) × (cycles/month) × (edge/cycle)**.
A small edge recycled fast = real money; the binding constraint is **deployable
edge per cycle = depth × breadth**, NOT % return. Optimise for high-turnover,
size-absorbing, broad opportunity — measure **deployable edge/day**.

## Scope — candidate edge areas (rough order of promise, given access)

1. **Cross-venue sports edges (PRIMARY):** soft-book-vs-exchange arb, value-betting
   vs the sharp (Pinnacle) closing line, exchange-vs-exchange, PM-vs-sharp
   mispricing. Natural home of the recycle model. Limited by book stake limits /
   gubbing (Pinnacle exempt).
2. **Polymarket LP / liquidity rewards** — market-neutral yield, untapped.
3. **Crypto spot (Coinbase)** — directional / stat-arb; free deep data for research,
   but competitive and no market-neutral edge for UK spot. Tertiary.

Prior: PM↔Betfair *convergence* (directional) was tested and **no variant survived
out-of-sample** — the easy directional version is picked over; need a sharper edge.

## Data sources & tooling (available now)

- **OddsPapi** (`ODDSPAPI_KEY` in `.env`) — base `https://api.oddspapi.io`, auth
  `?apiKey=`. **350+ bookmakers incl. Pinnacle (sharp), Bet365, soft books, Betfair
  Exchange**, 69 sports, live + **historical** odds. The core feed: sharp reference
  + soft books (value/arb) + exchange (hedge). Endpoints: `/v4/sports`,
  `/v4/tournaments?sportId=`, `/v4/odds-by-tournaments?bookmaker=&tournamentIds=`,
  `/v4/historical-odds`. Mind rate/quota.
- **API-Football** (`API_FOOTBALL_KEY`) — `https://v3.football.api-sports.io`, header
  `x-apisports-key`. **Pro plan, 7,500 req/day.** Fixtures, results, stats, odds for
  football — modelling + result resolution.
- **football-data.co.uk** — free CSVs, Pinnacle + soft closing odds + results for
  thousands of football matches. Zero-dep backtesting of value-vs-sharp.
- **Betfair** (have keys) live + downloadable Historic Data. **Polymarket** (have).
  **Coinbase** spot data (free public API).
- Re-use existing `src/` plumbing (executor, settlement, Betfair/PM clients) where
  it fits; add a backtest harness + re-enabled paper layer as the validation gate.

## Method — measurement-first

The make-or-break number for the recycle model is **deployable edge/day**, so
*measure before building*:

`measure opportunity (how much +EV / arb exists vs the sharp line, and the size it
absorbs) → if promising, backtest with results → paper-trade survivors → live pilot
(gated) → scale`. Kill fast; log every hypothesis (open / testing / killed / promoted
+ why) in [RESEARCH_LOG.md](RESEARCH_LOG.md).

## Methodology guardrails (hard rules — read before trusting any backtest)

### 1. Never peek into the future (no look-ahead bias)

**The selection signal — what tells you to place a bet — must use only data that
exists at the moment you would actually place it.** Settling on the real result is
fine (that's just measuring outcomes); the trap is letting *future* information
decide *which* bets to take or *what price* to assume.

- ✅ **Allowed:** filter/bet using the soft-book price and the sharp's price **as
  they are right now**; settle the bet on the actual match result.
- ❌ **Forbidden as a signal:** the **closing** line (you don't know it when betting
  early), the result, post-event info, or any value sampled later than your entry.
- The closing line is allowed **only as after-the-fact validation** (CLV — did my
  pick beat the eventual close?), never as a selection input.

**Worked example (a real trap we hit, 2026-06-14→15):** "bet the early soft price
when it beats Pinnacle's **closing** de-vigged line" backtested at **+6.3% ROI**.
But you can't see Pinnacle's close when betting early — that's look-ahead. The
implementable version, "early soft price vs Pinnacle's **early** line (both known
at bet time)," flipped to **−9% to −30% ROI**. The "edge" was almost entirely the
peek. Rule of thumb: if removing every value sampled *after* your entry timestamp
changes the result, the original result was fiction.

Other common look-ahead leaks to check for: using a same-row "average/max" that was
computed across the whole event window; survivorship (only events that still had a
price at close); resolving with odds revised after news broke; and train/test
contamination when fitting any model.

### 2. Avoid edges that depend on staying un-gubbed

Soft books **limit, ban, or "gub"** (stake-restrict) winning customers, often within
days/weeks. Treat the ability to keep getting a soft price as a **decaying, finite
resource**, not a steady-state assumption. Prefer edges that survive on
**non-gubbing venues** (sharp books like Pinnacle, and exchanges like
Betfair/Smarkets/Matchbook where you pay commission but don't get banned for
winning). When scoring a candidate, **explicitly state its gubbing exposure** and
**haircut capacity** for it: an edge that only exists at soft-book stake limits and
evaporates after a few hundred dollars of winning bets is **low-capacity by
construction** and should rank below an equally-sized edge on a non-gubbing venue.
Do not promote a strategy to "live" whose entire EV assumes soft books that will
restrict you before meaningful capital is deployed.

## Remaining operator inputs

- [ ] Token/time **budget** per cycle + check-in cadence (see Operating loop).
- [ ] Open **Pinnacle** + an exchange/soft book when a candidate is promoted to live.
- [ ] **Unwind** the current open arb book to free capital? (Hedged sports legs
      settle on their own; ~$47 pUSD parked in long-dated politics.) — not urgent.

## Operating loop (how this goal is run)

Run as a **recurring research cycle** (same pattern as the hourly matching runbook),
each iteration stateful via `RESEARCH_LOG.md`:

1. Read `GOAL.md` + `RESEARCH_LOG.md`; pick the highest-value next action.
2. Do **one** unit of work (measure / backtest / build), mindful of API quotas.
3. Append results to `RESEARCH_LOG.md`: hypotheses moved open→testing→killed/promoted,
   the current best-candidate ranking with EV evidence, and the next action.
4. Report a one-paragraph summary; **stop for sign-off before any real money**.

Cadence/budget set by the operator (e.g. an hourly/daily cron firing the research
runbook, or a self-paced `/loop`). Checkpoint: surface to the operator whenever a
candidate clears the success bar, a track is killed, or real funding/accounts are
needed.

## Current baseline (what exists)

Betfair↔Polymarket pre-match lock arb: real but small (~$20/mo, ceiling
~$1–1.5k/mo, depth-bound). Fully characterized in [FINDINGS.md](FINDINGS.md).
This goal is a deliberate step beyond it.
