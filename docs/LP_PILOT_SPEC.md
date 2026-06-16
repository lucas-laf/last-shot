# Polymarket LP (liquidity-reward) — live pilot spec

_Status: DRAFT for operator sign-off. Gated: requires real USDC + explicit "go".
Backs hypothesis **H4** (see [RESEARCH_LOG.md](RESEARCH_LOG.md)). Paper modelling is
done; this pilot measures the things the model can't._

## 1. Purpose — what this pilot is for

NOT to make money. To **measure three unknowns** that decide whether H4 scales to the
$5k/mo goal, and calibrate the model's haircut:

1. **Realized capture-rate** — actual reward earned ÷ the model's size-share estimate.
   (The model assumes reward ∝ posted size; Polymarket's real qScore rewards
   tightness-to-mid, so true capture is lower — by how much?)
2. **Inventory / jump P&L** — when our resting orders get filled, what do we lose (or
   gain) holding that inventory? Especially the tail: a news/resolution jump while we
   are a large fraction of a thin book.
3. **Operational reality** — can we keep ~N markets quoted within `max_spread` of a
   moving mid, with **polled** (not websocket) fills, at acceptable uptime?

**Exit deliverable:** realized net %/mo per market + aggregate, a calibrated
gross→net haircut, and a measured per-market capacity — enough to decide scale-up,
re-scope, or kill.

## 2. Minimum USDC to start — recommendation

Capital math: a two-sided quote of `S` shares locks ≈ **$S** USDC (post a buy on the
YES token at `p·S` + a buy on the NO token at `(1−p)·S` = `$S`; this avoids needing to
sell from inventory). Good markets have `min_size` ≈ 50–200 shares ⇒ **$50–200 to
enter one market**.

| Stage | USDC | What it buys | Goal |
|-------|------|--------------|------|
| **0 — plumbing** | **$100–150** | 1–2 low-`min_size` markets, 2–3 days | Prove place/cancel/replace, reward accrual, fill polling, settlement end-to-end. NOT a yield read. |
| **1 — measurement (recommended start)** | **$500** | 6–10 mean-reverting markets at/just above `min_size`, ~2 weeks, with inventory buffer | The real measurement of capture-rate + inventory P&L. |
| 2 — scale check | $2.5–5k | broaden to ~30–50 markets | Confirm capacity curve holds before full size |

**Recommended minimum to start: $500**, run **Stage 0 first on ~$150** for 2–3 days,
then top up to $500 for the 2-week Stage-1 measurement. Below ~$300 you can only enter
1–2 markets and can't separate a real signal from one market's idiosyncrasy/jump.
$500 is a hard, capped ceiling for the pilot — not a position to "let run".

Practical funding: USDC.e on **Polygon** in the Polymarket wallet (`POLY_FUNDER`); a
small amount of POL for gas; **V2 allowances approved** (`scripts/set_v2_allowances.py`)
— prerequisite, our V1→V2 migration note applies.

## 3. Market selection (the universe filter)

Quote only markets that pass ALL of:
- **`VR < 1`** (mean-reverting / noise-trader flow) on 31d hourly history — the
  LP-friendly condition from `analysis/lp_net_yield.py`.
- **`daily_pool ≥ ~$20`** and existing qualifying liquidity not already saturated
  (model net yield ≥ a sensible hurdle, e.g. ≥ 10%/mo).
- **Slow / long-dated, NO imminent event** — exclude anything resolving soon or with a
  scheduled catalyst inside the pilot window (live/just-before-kickoff sports, debates,
  earnings, election day). Jump risk is the killer; long-dated WC group-winners (no
  live match), far-off elections, policy markets are ideal.
- **`min_size` ≤ ~100** so $500 spans 6–10 markets.
- Sanity: real two-sided book, `accepting_orders`, tick size sane, not a near-dead
  market whose `VR<1` is just bid-ask bounce.

Seed list (re-validate at launch — prices move): Naftali Bennett next PM, various 2026
Senate/Gov races (Texas, Maine), long-dated 2027/2028 election markets, WC
group-winner markets for non-imminent groups. ~8 names, diversified across themes so
no single news event hits many at once.

## 4. Quoting mechanics

- **Two-sided via two BUY bids:** buy-YES at `mid − h`, buy-NO at `(1 − mid) − h`
  (equivalent to a YES ask at `mid + h`). Sidesteps PM's buy-only history — no selling
  from inventory required to be two-sided.
- **`h` (half-spread):** quote **tight to mid**, well inside `max_spread`, to maximise
  qScore. Start at `h ≈ min(1 tick, max_spread/3)`; the pilot will sweep `h` to measure
  the capture-rate vs adverse-fill tradeoff.
- **Size:** `min_size` (or slightly above) per side per market.
- **Re-quote loop:** every `refresh_s` (start 15–30s; bounded by fill-poll latency),
  cancel+replace any quote whose distance from the new mid would fall outside
  `max_spread` (reuse `maker_executor.quoting_loop` pattern + `reprice_eps`).
- **Fills are POLLED** (no v2 fill websocket) — `fill_poll_loop` every `poll_s`. Detected
  fills update inventory; this latency is itself a measured risk.

## 5. Inventory management (no external hedge)

Unlike the arb maker, there is **no Betfair leg** — a fill leaves directional inventory
on PM. Manage it on PM:
- **Per-market inventory cap** (e.g. ≤ 2–3× `min_size`, and ≤ 50% of book). On breach,
  stop adding to that side; **skew** quotes (widen/withdraw the side that grows
  inventory, keep the side that reduces it) to mean-revert toward flat.
- Optionally rest a reduce-only order to bleed inventory back near entry.
- Track inventory mark-to-mid continuously; it is the core of the net-P&L measurement.

## 6. Risk controls / kill-switch (hard)

- **Aggregate capital cap = pilot budget** ($500). Reserve-checked before every order
  (reuse maker capital-reservation). Never exceed.
- **Per-market caps:** size ≤ 50% of book; inventory ≤ cap above.
- **Loss kill-switch:** if cumulative realized+unrealized net ≤ **−15% of deployed**,
  halt + `cancel_all` + flatten where possible; alert operator.
- **Per-market jump halt:** if a market's mid moves > X% (e.g. 15%) in a poll interval,
  cancel that market's quotes immediately (don't refill into a moving market).
- **Event blackout:** auto-drop any market entering its resolution window / catalyst.
- **cancel_all on every exit path** (reuse maker exit discipline + startup cleanup of
  orphaned resting orders — never leave quotes live unattended).
- **Armed flag:** run in SHADOW (compute quotes, place nothing) first to validate logic;
  flip to armed only for the live stages.

## 7. Metrics (logged per market, per day)

reward earned (on-chain) · model size-share prediction · **capture-rate = actual/model**
· fills (count, size, side) · inventory path · inventory P&L (mark-to-mid) · fees/gas ·
**net = reward − inventory P&L − costs** · net %/mo annualised · uptime (% of time
quoted in-band). Aggregate to a calibrated gross→net haircut and a per-market capacity.

## 8. Infra: reuse vs build

- ✅ **Reuse:** `polymarket_executor.py` (V2 client, GTC resting orders, tick-snap,
  cancel_all, armed/shadow); `maker_executor.py` quoting/fill-poll/exit skeleton;
  capital reservation; storage/exec-event logging; `analysis/lp_net_yield.py` for
  selection; `scripts/set_v2_allowances.py`.
- 🔨 **Build (the delta):** an **LP quoting mode** — two-sided buy-YES/buy-NO reward
  quoting + inventory skew (no Betfair leg), market-selection wiring from the scanner,
  reward-accrual tracking, the kill-switch/jump-halt, and the per-market metrics logger.
  Estimate: a focused module mirroring `maker_executor`, ~1–2 days.
- ⚠️ **Confirm before arming:** PM **sell/NO-side order placement** works under V2 (we
  were buy-only); fill **polling latency** is acceptable; V2 allowances live.

## 9. Timeline & decision gate

Stage 0 (plumbing, ~$150, 2–3 days, mostly shadow→tiny-live) → Stage 1 (measurement,
$500, ~14 days). **Decision gate after Stage 1:** if realized net ≥ ~40–50% of model
gross AND no uncontrolled jump losses AND uptime acceptable → propose Stage 2 scale-up
with a sized $/mo projection. Else → re-scope or kill, documented in RESEARCH_LOG.

## 10. Operator sign-off checklist

- [ ] Approve **$500** pilot budget (Stage 0 $150 → Stage 1 $500), hard-capped.
- [ ] USDC.e on Polygon in `POLY_FUNDER`; POL for gas; **V2 allowances run**.
- [ ] Approve building the LP quoting module (1–2 days) before any live order.
- [ ] Confirm: SHADOW first, then armed; kill-switch thresholds above acceptable.
