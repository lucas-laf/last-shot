# Polymarket LP (Liquidity-Reward) Market-Making — Comprehensive Overview

_Current as of 2026-06-17. This is the **active approach**. Earlier cross-venue
sports/arb work is historical and not relevant — see "Superseded work" at the bottom._

---

## 1. The thesis (what this is)

Polymarket runs a **liquidity-rewards program**: it pays a fixed pot of USDC **every
day, per eligible market**, split among makers who post resting two-sided limit orders
near the midpoint. The strategy is to **harvest that daily subsidy** on **slow,
mean-reverting markets** where the cost of providing liquidity (adverse selection) is
low. It is roughly **market-neutral income** — you're not betting on outcomes, you're
renting out liquidity and collecting the bribe.

Why this approach at all: the prior "beat the sharp betting line" edge-hunt (value
betting, cross-venue arb) was tested and **killed** (see Superseded work). Polymarket
LP was the one surviving candidate — no gubbing, no sports seasonality, untapped.

## 2. How the reward mechanism works

Each reward market publishes three parameters (pulled live from `clob.polymarket.com`):
- **`rewards_daily_rate`** — the USDC pot paid out that day (e.g. $273/day on a busy market).
- **`min_size`** — minimum order size to qualify at all (commonly 20–200 shares).
- **`max_spread`** — how close to mid your order must sit (commonly 3.5–5.5¢).

Your slice of the pot ≈ your **qScore / everyone's qScore**, where qScore rewards
**size × closeness-to-mid × two-sidedness**. Key properties:
- **Rewards are paid daily, independent of resolution.** You don't wait for the market
  to settle. Capital is recyclable (cancel and redeploy any time).
- **The cost/risk is inventory.** When your resting order fills you hold a position; if
  the price then moves against you, that inventory P&L offsets the reward. **Net edge =
  reward − adverse-selection/inventory cost.** Hence we only want mean-reverting markets.

## 3. What we built

| Component | File | Purpose |
|---|---|---|
| LP executor | `src/execution/lp_executor.py` | The bot: loops for book-refresh, quoting, fill-poll, reward-poll, metrics. Two-sided GTC quoting (buy-YES + buy-NO), inventory skew, jump-halt, kill-switch, hard budget cap, **startup reconciliation**. |
| Runner | `src/execution/lp_run.py` | Standalone entrypoint. Market selection (`scan_candidates` + quotability filter), on-chain position reconciliation, wires the loops, cancel-all on exit. |
| Shadow/backtest | `src/execution/lp_quoter.py` | Pre-live: live snapshot, 31-day **replay backtest**, candidate scanning + variance-ratio. |
| Config | `config.yaml` → `execution.lp` | Budget, cadences, risk knobs, selection params. |
| Detached wrapper | `scripts/lp_overnight.sh` | `setsid`/`nohup` runner + auto-restart (safe once reconciliation lands). |
| Spec | `docs/LP_PILOT_SPEC.md` | The original pilot design. |

**Safety rails (all live):** hard budget cap (reserved + Σ inventory cost-basis ≤ budget);
per-market ≤50%-of-book and inventory caps; per-market jump-halt; aggregate −15%
kill-switch; cancel-all on every exit; startup orphan cleanup; and **startup
reconciliation** (loads held positions, counts their capital against budget, seeds
inventory) so the cap holds across restarts and the bot never stacks a fresh budget on
top of existing positions. All metrics/fills/alerts log to the `exec_events` SQLite table.

## 4. Paper / backtest results (pre-live)

- **Gross pool is large:** ~**$513k/month** across 1,000+ reward markets.
- **Slow markets are mean-reverting:** ~**80%** of slow markets had variance-ratio < 1
  (LP-friendly; trending markets correctly modelled as money-losers).
- **Capacity saturates:** modelled $/mo rises with capital but %/yield falls steeply.
  Conservative 31-day replay (min-size sizing, inventory drag subtracted, optimistic
  capture) netted ~**$2–2.5k/mo at ~$5k+ deployed**.
- Honest pre-live verdict: likely a **modest** edge; the original **$5k/mo goal was
  already judged unlikely** (base case ~$1–3k/mo at $25k).

## 5. Live pilot results (the real data)

Staged ramp: **0a** shadow → **0b** ($50, 1 mkt) → **0c** ($150, ~6 mkts). All on the
armed box (V2 client, allowances set, ~$424 USDC funded).

- **Stage 0b validated the mechanics live:** two-sided quoting, fill detection, and
  inventory tracking all work. First fill landed slightly profitable.
- **Stage 0c ran detached at $150** across ~6 markets + a held position; **startup
  reconciliation proven** (held capital counted, cap held at ~$135/$150).
- **The make-or-break number — reward capture-rate:**
  - **6/16 full-day settled: 34%** (actual on-chain reward $2.36 vs model $6.99). The
    size-share model is ~**3× optimistic**. Intraday reads of 48–57% were inflated by
    the lagging reward poll; the settled figure is authoritative.
  - **6/17: the reward rate then collapsed ~5×** ($0.43/hr → $0.08/hr) as inventory built.
- **Totals across the pilot:** ~**$2.85** of reward earned; inventory P&L ≈ **+$0.37**
  (dominated by an AJ position that drifted up — directional luck, not the edge).

## 6. The key finding — why it underperformed

**Selection picked the highest-*pool* markets, which were extreme-priced (~$0.10 / ~$0.89)
nominee/crypto markets — the worst possible markets for LP.** On markets that close to
0 or 1, order flow is **one-directional** (everyone hits the same side). The bot:
1. accumulates directional inventory on one side,
2. **can't flatten** (no one trades the other side back to us),
3. ends up **quoting one-sided** → low qScore → **reward collapses**,
4. and the stuck inventory marks slightly against us.

Secondary finding: **the inventory cap is *soft*** — it stops *placing* the capped side,
but an in-flight resting order can fill and **overshoot** (we hit −67.6 / +68.4 vs a ±60
cap). Dollar risk stayed trivial (~$13 of inventory at risk, kill-switch never close),
but the mechanism clearly degraded the edge.

## 7. Honest economics / outlook

- Live capture **34% and falling** under inventory load ⇒ the realistic sustained edge
  is at the **low end of every estimate: ~$1–2k/mo** at sensible capital, **not $2.5k+**,
  and the **$5k/mo goal is effectively ruled out** for the naive version.
- The edge is **real but thin**, and a meaningful chunk of the modelled gross is eaten by
  inventory drag that the naive market selection actively *invites*.
- Pilot capital risk was negligible throughout (~$118 of inventory, ≈ break-even).

## 8. Required refinements before this is viable (the real to-do)

1. **Price-band filter (the big one):** only quote markets roughly in **0.15–0.85**.
   Extreme-priced markets are the inventory trap; avoiding them should lift capture
   substantially and is the single highest-value change.
2. **Harden the inventory cap:** cancel the opposite-side quote as inventory nears the
   cap (don't rely on the soft skip that overshoots).
3. **Active flatten / reduce-only path:** a way to bleed stuck inventory (needs a V2
   *sell*, which is untested — only buy-YES/buy-NO are proven so far).
4. **Down-weight / skip one-directional-flow markets** in selection.
5. (Done) reward-poll wired to the real `get_earnings_for_user_for_day(date)`.

## 9. Current status (2026-06-17)

- **Bot STOPPED**; wrapper killed (no auto-restart); **all resting orders cancelled**
  (open orders = 0); fill/alert monitors stopped.
- **Held inventory remains** (~**$118** current value, net P&L ≈ **+$0.37**): 50 YES AJ
  Dybantsa (+$3.25) plus small ~break-even positions in the nominee/crypto markets.
  These sit until they resolve or are manually closed (no auto-sell in the pilot).
- Code preserved on branch **`lp-rewards-pilot`** (pushed to origin).

## 10. Verdict & recommendation

**The mechanism works, but the naive bot underperforms** because its market selection
walks straight into the inventory trap. It is **not validated** as a $2k+/mo edge; live
evidence points to a **thin ~$1–2k/mo** edge at best, with the original $5k/mo target out
of reach. The decision is whether the **refinements (esp. the price-band filter)** can
lift capture enough to make a thin-but-real market-neutral income stream worth the
operational complexity — which would require **one more clean test** with those changes —
**or** to conclude the edge is too thin to pursue and wind down. No further capital should
be deployed until the price-band + cap fixes are in and re-tested.

---

## Superseded work (historical — NOT the current approach)

The following were tested earlier and are **not relevant** to the LP approach above. Kept
for the record only:

- **Cross-venue Betfair↔Polymarket arbitrage** (the original project) — real but
  structurally tiny (~$20/mo, ceiling ~$0.5–1.5k/mo, depth-bound); in-play blocked by
  both venues' order delays. See **`docs/FINDINGS.md`** and **`STATUS.md`**.
- **The "$5k/mo edge-hunt"** (goal + hypotheses H1–H5) — **`docs/GOAL.md`** and the
  pre-2026-06-15 entries of **`docs/RESEARCH_LOG.md`**. Outcome: **H1** soft-book value
  betting **killed** (the +EV was look-ahead bias); **H2** Pinnacle↔Betfair value
  **killed** (two sharp venues, no edge); **H3** convergence and **H5** crypto not pursued.
  The hunt **concluded** that Polymarket LP (H4) was the only survivor — which is this doc.
- Backtest scripts for the dead tracks: `analysis/value_vs_sharp.py` (H1),
  `analysis/arb_scan.py` (H2). Retained but not part of the LP path.
