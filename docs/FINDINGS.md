# last_shot — Findings & Status

_As of 2026-06-14. Cross-venue arbitrage between Betfair (exchange) and
Polymarket (CLOB), live-armed on EC2 (i-068384d6dcb71bee2), min stakes._

## TL;DR

The arbitrage edge is **real but structurally small**. Cross-venue in-play sports
arb is **blocked by design on both venues**; the only place the mechanics work is
**pre-match and slow non-live markets** (e.g. politics). At current size the taker
makes **~$20/month**; the ceiling even with unlimited capital is **~$0.5–1.5k/month**,
bounded by thin Polymarket depth — not by our wallet. The maker side has **no
viable regime**. Realized PnL to date is **−$4.92**, entirely from in-play hedge
failures that a pre-match gate (now deployed) prevents going forward.

## What was built

- **Live two-leg executor** (`src/execution/`): taker fires a Polymarket FOK on the
  perishable leg, hedges on Betfair sized to the actual fill; both-legs-or-unwind.
  NO-token short unlocks PM-sell arbs. Bounded by min stakes + daily capital +
  one-shot. Migrated to **Polymarket CLOB V2** (sig_type 3 / pUSD).
- **Maker executor** (`maker_executor.py`): rests passive GTC quotes inside the PM
  spread, hedges on fill. Built, tested, deployed — see verdict below.
- **Discovery + matching** pipeline with an in-session human reviewer (no API key):
  hourly discovery → candidate export → equivalence review → ingest → auto-promote.
- **Settlement** (`settle_live.py`): resolves both legs from each venue's own result,
  flags divergence (one venue voids while the other pays).
- **Analysis** (`analysis/`): `dislocation_duration` (window-lifetime study),
  `capturable_edge` (unlimited-capital ceiling), capture/edge reports.

## The core finding: both venues delay in-play marketable orders

This is the load-bearing discovery and it explains everything else.

| | entry (taker FOK) | hedge (maker / taker hedge) |
|---|---|---|
| **Polymarket** | `seconds_delay` **1s** on live sports holds the FOK → matches ~0 | resting GTC orders are exempt |
| **Betfair** | (fast pre-match) | `bet_delay` **3–5s** in-play holds the FOK → expires |

Both are deliberate anti-latency-arbitrage mechanisms on live in-play sports.
A FILL-OR-KILL cannot survive a multi-second hold, so:

- **Taker entry** dies on Polymarket's delay: tennis FOK fill rate **9%** (4 matched
  / 37 delayed / 1 killed) vs **politics 75%** (no delay, slow market).
- **Taker/maker hedge** dies on Betfair's delay: in-play hedges expired at a measured
  3.1–3.2s "RTT" (it was the bet delay, not network), forcing loss-making unwinds.
- **Pre-match, both delays are zero** — `bet_delay=0` before in-play, and PM fills.
  Of our 4 tennis taker fills, **3 were pre-match**.

Corroborating: tennis dislocation windows last **~1.7s median, 96% >94ms** (our PM
order round-trip) — so **latency was never the barrier**. Speeding up the taker
does nothing; the venues' delays are the wall.

**Conclusion: in-play live sports is a dead zone on both legs. The viable surface
is pre-match + slow non-live markets.**

## Economics

- **Per clean lock: ~$0.16** profit at min stakes (~$2.50 notional, ~2.6% edge).
- **Two regimes**: pre-match **sports** settle in hours (capital recycles);
  **politics** settles in months (capital parks until the election).
- **Now (min stakes, capital-constrained): ~$20/month**, opportunity-bound (~3–4
  recyclable sports locks/day). Politics is a one-time harvest, not income.
- **Unlimited capital: ~$0.5–1.5k/month.** Raw capturable-edge sweep gave ~$9k/mo
  gross but **67% was two stale-quote outlier markets**; stripping them → ~$3k/mo
  gross; after phantom-liquidity (PM displayed depth doesn't fully fill) and
  market-impact haircuts → low four figures at most.
- **Binding constraint is available edge, not capital.** `min(PM, BF)` depth is
  almost always the **Polymarket** side, and PM pre-match books are thin. More money
  saturates fast; only **more market coverage** grows the edge pool.

## Strategy verdicts

- **Taker — viable but tiny.** Works where it can fill and hedge: slow/non-live
  (politics 75%) and pre-match sports. Edge is real (every clean lock settled
  positive). Capped by thin opportunity + min-stake size.
- **Maker — no viable regime.** In-play it can't hedge (Betfair bet delay; two live
  tests expired, unwound at −$0.05 and −$0.20). Pre-match it can't even quote:
  pre-match tennis almost never offers a ≥2% lockable spread inside the PM book
  (~1 quotable moment/hour observed). It is *held*.

## Reliability & safety (current)

- **Pre-match gate** on both executors (`_pre_match`: trade only when the Betfair
  market hasn't started). Stops the in-play hedge-expiry losses at the source.
- **Taker error-path now unwinds** a naked PM leg instead of flagging `error` and
  walking away (the #53 Scotland bug, −$2.40).
- **settle_live resolves stuck `error` rows** — no more settlement blind spot.
- **0 divergences** to date. First void event (Tommy Paul) resolved cleanly (both
  venues voided in sync → $0). Walkover risk unrealized so far.
- Committed at `7d32897`; 66 tests pass. `config.yaml` stays box-local (`armed:true`).

## Capital

- **Total ~$1,127**: Polymarket ~$84 (scarce), Betfair ~£821 / ~$1,043 (barely used).
- **pUSD is the binding side and is slowly draining**: all 17 open locks are
  long-dated **politics**, parking pUSD until elections months away. ~$34 left ≈ ~13
  more min-stake arbs before the capital gate throttles. Sports locks recycle fine.
- Judge PnL by the **two-venue total + settled realized (−$4.92)**, never by one
  venue's balance — cross-venue settlement moves cash between venues.

## Open decisions (no open technical unknowns)

1. **Maker**: shelve (verdict reached) vs. force one low-margin fill to confirm
   hedge plumbing (won't change the economics).
2. **Capital**: stop parking pUSD in long-dated politics — the constraint biting now.
3. **Direction**: low-touch "let it run + measure net PnL," invest in scaling
   (bigger stakes / broader coverage), or wind down.

## Key parameters

- `MIN_EDGE=0.01` (raised from the convergence-era 0.0025), Betfair `commission=0.02`.
- Taker: min stakes (~£2/$2.5 legs), one_shot off (soak), pre-match gated.
- Maker: margin 0.02, poll_s 0.25, one_shot, pre-match gated — held.
