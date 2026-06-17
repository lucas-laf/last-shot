# Research Log — edge-hunt toward mid-4-figures/month

> ℹ️ **Current approach = Polymarket LP rewards — see [LP_OVERVIEW.md](LP_OVERVIEW.md).**
> This is the full chronological journal. Entries from **2026-06-15 onward** are the LP
> pilot (current); earlier entries cover the **now-killed sharp-line edge-hunt**
> (H1/H2/H3/H5) and are historical.

Running state for the [GOAL.md](GOAL.md) research loop. Each cycle: read this,
do one unit of work, append results, update the ranking + next action.
Newest entries at the top of the Journal.

## Best-candidate ranking (live)

| # | candidate | status | est. $/mo @ $25k | confidence | evidence |
|---|-----------|--------|------------------|------------|----------|
| 1 | **H4 Polymarket LP / liquidity rewards** | testing — **promising; net-screen positive** | conservative **~$2–2.5k/mo** (min_size sizing, 150-mkt scan, incl. inventory drag); optimistic higher w/ broader scan + bigger size | med on *direction*, low on *realized net & $5k reachability* | shadow 31d replay: net saturates ~$6k deployed/$2.4k/mo; reward engine − inventory drag; thin-mkt % inflated by modelled over-capture |
| — | **H2 Pinnacle↔Betfair value** | **killed** (Shin de-vig) | ~£20 EV on peak WC day | — | bias-corrected liquid edge = +2.7/+1.2/+0.5%; the £31k-deep selection = +0.5%. Two sharp venues ⇒ no systematic value. |
| — | H1 "early soft vs Pinnacle CLOSING" | **killed (look-ahead)** | — | — | +6.3% ROI was hindsight; removing the peek → −9 to −30% |
| — | H1 closing-vs-closing variant | **killed** | — | — | −1 to −16% ROI; efficient at close |

## Hypothesis backlog

| id | hypothesis | track | status | note |
|----|-----------|-------|--------|------|
| H1 | Soft-book / exchange odds are systematically beatable vs Pinnacle's de-vigged line (value betting) | sports | open (naive form killed) | Look-ahead-free test (early soft vs Pinnacle EARLY) is −EV on EPL 23/24. Only a genuine point-in-time stale-price signal could revive it; no peeking at the close. |
| H2 | Cross-venue **arbs** (best back across books vs exchange lay; inverse-odds sum < 1) exist at deployable size/frequency | sports | value form **killed**; pure-arb form untested | Value-vs-sharp on Betfair = efficient (Shin-corrected edge ~0–2% on negligible size). Pure riskless back/lay arb not yet measured, but baseline already says small/depth-bound. |
| H3 | Polymarket lags the sharp (Pinnacle) line on shared events — directional convergence (NB: prior convergence test failed OOS) | sports/PM | open | needs a sharper signal than before |
| H4 | Polymarket LP / liquidity rewards = positive net yield after hedging inventory | PM | testing | Gross pool $513k+/mo; gross yield/$ 15–30%/mo on slow markets, top markets farmed. NET (after adverse selection) is the open question. |
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

### 2026-06-14 — H1 first backtest (free data, zero API quota)
- Built `analysis/value_vs_sharp.py`. Tested on the one season already on disk
  (`research/data/E0_2324.csv`, EPL 2023/24, 380 matches). Method: de-vig Pinnacle
  CLOSING odds (proportional) → fair probs; bet flat $1 when `price·p − 1 ≥ thr`;
  settle on FTR. Pinnacle mean overround 1.029.
- **Result A — closing price vs closing fair (efficiency check): KILLED.** ROI
  −0.96% (thr 0) → −15.8% (thr 0.05), worsening with selectivity even though CLV is
  +6.8→+11.9%. Signature of (i) proportional de-vig overstating longshots
  (favourite-longshot bias) and (ii) the cross-book max at close being an
  efficient-market artifact. The naive "value at close" version does not pay.
- **Result B — EARLY price vs Pinnacle CLOSING fair (real value-bet entry): +EV.**
  `MaxE` (best early book price) **+6.32% ROI / 592 bets** (thr 0), tapering to ~0 by
  thr 0.05 (noise). `AvgE` (AVERAGE early book) is the robust read: **+4.2% → +7.6%
  ROI** monotone-ish across thresholds, **hit 37–40%**, 81–277 bets, CLV +7→+13%.
  The *average* book beating Pinnacle's close (not a cherry-picked outlier) ⇒ the
  signal is broadly capturable, not one stale book.
- **Interpretation:** H1's real mechanic = harvest the soft-book line BEFORE it
  converges to the sharp close. In-sample +4–7% ROI net of price (vig already in the
  realized P&L). Caveats: single league/season (wide error bars, no OOS), no
  drawdown/capacity yet, and **gubbing/stake limits are the binding capacity
  constraint** (Pinnacle exempt but Pinnacle is the reference, not a target book).
- Status: H1 open→**testing**; closing-vs-closing sub-variant **killed**.
- **Next action:** out-of-sample confirmation — download more free football-data.co.uk
  seasons + leagues (E0 multi-season, plus D1/I1/SP1/F1), re-run `value_vs_sharp.py`
  pooled, and check the AvgE/MaxE early-vs-close ROI holds (target: stable >+2% net
  across leagues/seasons). Then scope capacity (bets/day × viable stake pre-gubbing).

### 2026-06-15 — H1 look-ahead correction (operator caught the peek)
- Operator question: "would we know the Pinnacle CLOSING line at the time we bet the
  soft book?" **No.** Yesterday's +EV used Pinnacle CLOSING to *select* bets placed
  early — look-ahead in the selection rule (P&L still settled on FTR, but the choice
  of bets peeked at the future).
- Re-ran with both sides known at bet time: fair = **Pinnacle EARLY** (`PSE`),
  price = early soft. Results (EPL 23/24):
  - MaxE vs Pinnacle EARLY: **−9.4% (thr 0) → −37.5% (thr 0.05)**, hit ≤23%. Dead.
  - AvgE vs Pinnacle EARLY: tiny n (≤18 bets), noise, no usable edge.
  - **Bet365 early vs Pinnacle EARLY** (one real soft book): +22% @ 47 bets / +27% @
    33 bets at low thr, but n is tiny and CLV ≈ 0 → almost certainly noise, not a real
    signal. Flagged for OOS, not believed.
- **Verdict:** the naive H1 value bet is **killed** once the peek is removed. The
  +6.3% headline was hindsight. H1 only revives via a *genuine point-in-time* stale
  soft price vs the contemporaneous sharp — and so far that's −EV on this season.
- Added **Methodology guardrails** to `GOAL.md`: (1) no look-ahead — selection signal
  must use only data available at entry; closing line is validation (CLV) only, never
  an input; with this exact worked example. (2) avoid gubbing-dependent edges —
  prefer non-gubbing venues (Pinnacle, exchanges); haircut capacity for soft-book
  ban risk; don't promote an edge whose EV assumes soft books that will restrict you.
- **Next action:** pivot off naive value-betting. Higher-value: test **H2 (arbs:
  best back vs exchange lay, inverse-odds sum < 1)** and **H3/exchange-based** signals
  on **non-gubbing venues** — measure point-in-time, no closing-line inputs. Use a
  live OddsPapi snapshot (Pinnacle + Betfair + soft) to count arbs and their size.

### 2026-06-15 (cont.2) — built `analysis/arb_scan.py`; first non-gubbing measurement
- Built the scanner the operator approved (path A). Pieces: OddsPapi Pinnacle 1X2
  (canonical market id **`101`** = period-0 full-time moneyline; `home/draw/away`
  outcome ids; `limit` = depth) → de-vig → fair; matched to **own Betfair** live
  Match-Odds best back/lay via `list_market_book` + `EX_BEST_OFFERS`; team names from
  cached `/v4/participants?sportId=10` (18,208 teams). Commission 2% from config.
- **Run on World Cup (tid 16), a peak 60-fixture day:** matched 58/60 to Betfair,
  41 back-value outcomes at face value. **But applied the no-overclaim guardrail:**
  - Edge by odds band: fav<2.5 **+1.0%** (n2), mid 2.5–6 **+3.6%** (n14),
    long>6 **+9.6%** (n25). Edge concentrated in **longshots** = favourite-longshot
    de-vig artifact + thin top-of-book signature, not necessarily real.
  - **Deployable subset** (Betfair matched >£50k so its price is meaningful, fair
    odds <6, size ≥£20): only **4 selections**, edges **+5.1 / +3.3 / +0.1 / +0.0%**.
    On liquid markets the value-vs-sharp edge **largely evaporates**.
- **Verdict:** mechanism works and is measurable, but the naive multiplicative-de-vig
  version shows the edge is mostly artifact; the credible liquid edge is **small,
  sparse (~2 real selections / 60 fixtures), and unconfirmed** (could be residual
  de-vig bias on away/mid outcomes). Nowhere near the $5k/mo bar yet. H2 stays
  **testing — weak**, now the #1 (only) live candidate by default.
- Method note: top-of-book level-1 only (overstates size); should use VWAP over the
  depth actually taken. Liquid WC markets ≈ sharp ⇒ little edge; thin markets show
  gaps but no capacity — the central tension for this pair.
- **Next action (decisive):** (1) replace multiplicative de-vig with **Shin** (or
  power/odds-ratio) de-vig and re-run — if the +3–5% liquid mid-odds edge survives
  the favourite-longshot correction it's a real candidate; if it vanishes, H2-value
  is **killed**. (2) If it survives, stand up a **forward paper-settlement log**
  (record selections now, settle via API-Football/Betfair after KO) for realized ROI
  — the only look-ahead-free proof. (3) Consider Betfair-vs-Pinnacle **arb** (lay side)
  separately. Also keep H4 (PM LP) queued as the season-independent alternative.

### 2026-06-17 (cont.2) — WOUND DOWN: bot stopped, all positions closed, net +$0.64
- Operator stopped the run; bot killed, all resting orders cancelled (0 open).
- **Validated the V2 SELL path** (previously untested) — test sell 3 AJ @0.79 matched,
  then liquidated all 8 holdings into the books. **0 positions, 0 open orders** left.
- **Final realized: net +$0.64** (USDC $424.55→$425.19; ~$0.49 6/17 reward still to
  settle → ~+$1.1). Liquidation realized ~−$2 trading loss (inventory drag crystallized;
  AJ +~$3.5 offsetting nominee losses) but ~$2.85 rewards covered it. **Pilot was
  marginally net-positive — it cost ~nothing.** Comprehensive writeup: docs/LP_OVERVIEW.md.
- Open decision (task #11) unchanged: refine (price-band filter etc.) + re-test, or wind
  down for good. Nothing currently deployed or at risk.

### 2026-06-17 (cont.) — inventory dynamics + soft-cap finding (Stage-1 refinements)
- Overnight, low-price nominee markets accumulated directional inventory from
  persistent one-sided flow (Grace Meng repeatedly filled NO → net short YES).
  **Inventory cap is SOFT:** skew stops *placing* the capped side at ±(inv_cap_mult×
  min_size)=±60, but an in-flight resting order can fill and overshoot — Grace Meng hit
  **−67.6** (13% past −60). Dollar risk trivial (~$6.76 max), kill-switch clear, deployed
  pinned ~$148/150.
- **Finding = the live cause of the 34% capture:** one-sided flow → one-sided quoting →
  low qScore + inventory marks slightly against us (trading_pnl −$0.64 as positions built).
  And one-directional flow can leave inventory **stuck** (no one sells us the offsetting
  side to flatten).
- **Stage-1 refinements (before scaling):** (a) HARDEN the inventory cap — cancel the
  opposite-side quote as inventory nears the cap (don't rely on soft skip); (b) consider
  an active flatten (reduce-only) path; (c) maybe down-weight/skip markets with strong
  one-directional flow. None urgent at $150; important at $500+.

### 2026-06-17 — full-day SETTLED capture-rate: 34% (below gate; tempers outlook)
- 6/16 settled on-chain reward **$2.36** across 5 mkts; model accrued **$6.99** ⇒
  **full-day capture-rate 34%** (intraday 48–57% were inflated by the lagging reward
  poll catching up in bursts; settled is authoritative).
- **Implication:** size-share model ~3× optimistic (not 2×). Gross×0.34: $5k≈$1.7k/mo,
  $25k≈$3.9k/mo ⇒ **$2–2.5k/mo now needs ~$7–10k**; $5k/mo goal even less likely.
  Realistic picture moves to the conservative end: **a ~$1–2k/mo edge**, not $2.5k+.
- Caveats keeping it alive: (1) only a PARTIAL day (~5.5h); (2) **inventory drag** — as
  positions built the bot quoted one-sided → lower qScore, while the model assumes
  perfect two-sided, so it over-counts exactly when holding inventory; (3) 34% >> the
  <20% kill line — edge is real, just smaller.
- **Decision: do NOT gate Stage 1 on this partial day.** Run a clean full 24h (6/17),
  re-read capture. ~34% sustained ⇒ reset expectations to ~$1–2k/mo; recovery to 45%+ ⇒
  better case back. Bot healthy/stable (uptime 2h+, 0 crashes, 0 alerts, cap held).

### 2026-06-16 (cont.2) — Stage 0c LIVE & DETACHED ($150), reconciliation proven
- Bumped budget_usd 50→150; launched **detached** via `setsid nohup scripts/lp_overnight.sh`
  (survives session, auto-restart now safe with reconciliation).
- Startup reconciliation worked in production: held AJ **$36 counted → $114 free**, 52
  scanned / 44 quotable / 6 active = **5 new markets + AJ held**. Deployed **$134.72 ≤ $150**
  (reserved + Σ cost_basis) — cap holds, no stacking. New markets are high-pool political
  nominee mkts (Grace Meng $273/d, Lasher $246/d, Park $227/d) + Ostium; all min_size 20.
- Quoting two-sided live; first fill within seconds (Grace Meng YES @0.892). net +$3.99,
  kill-switch clear. Persistent fill/alert monitor running.
- **Now soaking** for the real multi-market, multi-day capture-rate + inventory-P&L read.
  Next: check on-chain rewards (get_earnings_for_user_for_day) over the next day(s);
  decide Stage 1 ($500) at the gate (capture ≥ ~40-50% of model, inventory controlled).

### 2026-06-16 (cont.) — startup reconciliation BUILT (cap now holds across restarts)
- Fixed the unattended-blocker. `lp_run` now, on launch: reads our LP `condition_id`s
  from `lp_fill` events, fetches ON-CHAIN positions (data-api), attributes them to LP
  (vs legacy wallet holdings), and: (a) **counts committed capital against budget**
  (`available = budget − committed`), (b) **seeds MarketState inventory** for held
  markets, (c) excludes held markets from NEW allocation, (d) tracks held-but-unscanned
  positions as count-only states. `_deployed_usd` (reserved + Σ cost_basis) + the
  executor budget gate enforce the cap. Also wired `_fetch_onchain_rewards` to the real
  `get_earnings_for_user_for_day(date)`.
- **Validated (dry-run, no money):** detected held AJ (50 YES @0.72 = **$36 committed**),
  available **$14**, **0 new markets funded** (can't fit a $50 min_size) ⇒ a relaunch
  no longer stacks a fresh $50 on top. Cap holds. (AJ fell to count-only this scan as it
  wasn't in the candidate set; it's counted + held, not actively flattened.)
- **Resume decision still needed (operator):** at $50 budget, $36 is locked in AJ so only
  $14 is free → the bot would open ~nothing new. To run a real Stage 0b/0c either
  (a) flatten AJ to free the $36 (needs a V2 *sell*, untested), (b) raise budget to ~$86
  ($36 held + $50 new), or (c) accept it just holds AJ. Then relaunch (detached).

### 2026-06-16 — first on-chain capture-rate datapoint (tiny but ENCOURAGING)
- Pulled actual earnings via V2 client `get_earnings_for_user_for_day`. **2026-06-15:
  $0.047978** from the AJ market (condition 0xaf13295...); 2026-06-16: $0 (bot stopped).
- Capture-rate calc: orders qualified ~8 min two-sided. Pool flow in window ≈ $27 ×
  8/1440 ≈ $0.15 → **realized share ~32%** vs **model ~30%** (50 vs ~118 competing) ⇒
  **capture ≈ ~100% (1.07×) of model.** Better than the hedged "capture << model" fear.
- HEAVY caveat: 4.8¢ over 8 min = tiny/noisy; doesn't test full-day capture (competition
  varies) or inventory cost. One datapoint, not a verdict. Real read needs a full day.
- Reward methods confirmed on V2 client: get_earnings_for_user_for_day(date),
  get_total_earnings_for_user_for_day, get_user_earnings_and_markets_config — wire these
  into `_fetch_onchain_rewards` (replace the getattr guesswork).

### 2026-06-15 (cont.14) — Stage 0b first FILL + parked safe overnight
- The AJ Dybantsa YES quote **filled** (~8 min resting): bought 50 YES @0.72, mid now
  0.735 → **+$0.75** (favourable — mean-reversion thesis working). Fill detection +
  inventory tracking **validated live**. This is the key Stage-0b mechanic proven.
- Process-management quirk: the run_in_background bot reparents to init (PPID 1) and
  survives; the harness marked its task "failed" while the process kept running (hence
  the fill). Cleaned up: **no bot running, no resting orders** (confirmed open_orders=[]).
- **Gap found (blocks unattended run):** the bot does NOT reconcile pre-existing
  positions/orders on startup. Relaunching would deploy a fresh $50 ON TOP of the held
  50 YES → ~$86 exposure, breaching the $50 cap. Clean reset needs a V2 *sell* (untested).
- **Decision (operator): hold-and-fix-tomorrow.** Holding 50 YES AJ @0.72 (~$36, bounded;
  won't resolve overnight — NBA draft later). No bot overnight. Monitor stopped.
- **Next (tomorrow, daylight): build startup reconciliation** — on launch, load open
  orders + on-chain positions, seed MarketState inventory, count committed capital toward
  budget so the cap holds across restarts. THEN resume Stage 0b/0c unattended. Also: get
  the first on-chain reward datapoint for the ~8 min the orders rested (capture-rate).

### 2026-06-15 (cont.13) — Stage 0b LIVE: first real LP quotes resting on-chain
- Operator gave the launch go. First armed launch found a selection bug: `allocate()`
  picked purely by yield and handed the whole $50 to a blacked-out, thin market
  (Baldacci) → bot ran healthy but idle (0 quotes). Fixed: added `_quotable()`
  pre-filter in `lp_run` (drop blacked-out / thin-book candidates before allocate,
  1.5x book-depth headroom). Killed the idle bot cleanly (0 open orders confirmed).
- **Relaunched — Stage 0b LIVE**: 16 scanned → 11 quotable → funded **AJ Dybantsa
  first-pick** (pool $27/d, mid 0.73). Posted **two-sided live GTC**: buy-YES @0.72 +
  buy-NO @0.26, 50 sh each, both `status:live`. **$49/$50 deployed, quotes 2.** First
  real LP capital at risk; qualifying for rewards.
- Persistent monitor armed (fills / jump-halt / kill-switch alerts). Runs unattended.
- **Next:** tomorrow check on-chain reward accrual (rewards/user/markets) for the AJ
  market = first real **capture-rate** datapoint; watch inventory/fills. If healthy →
  Stage 0c (budget_usd 150, 3-5 markets). Hard max loss this stage ≈ $50.

### 2026-06-15 (cont.12) — LP bot built + shadow-validated + pre-flight GREEN
- Plan approved (operator: ramp $50→$150→$500, unattended). Built `src/execution/lp_executor.py`
  (LpExecutor: book_refresh/quoting/fill_poll/reward_poll/metrics loops, two-sided buy-YES/buy-NO
  GTC, inventory skew, jump-halt, aggregate kill-switch, budget reservation, cancel_all on exit)
  + `src/execution/lp_run.py` (standalone runner) + `execution.lp` config block. Enriched
  `lp_quoter.scan_candidates` with no_token/condition_id/end_date.
- Logic unit-tested (target pricing, skew, eligibility, jump-halt, fill accounting, kill-switch
  all pass). Fixed a book-share-gate bug (`max(book_side,size)` → market-level thinness gate);
  confirmed clean **two-sided** shadow quoting + capital reservation + cancel_all.
- **Pre-flight GREEN on the armed box:** py_clob_client_v2 installed; POLY creds present; **USDC
  balance $424.55**; V2 allowances already set (USDC+CTF max). Dry-run sign OK on a neg-risk
  reward token. **Armed acceptance test posted LIVE + cancelled cleanly for BOTH YES and NO
  tokens** — closes the open "V2 NO-side" question. (api-key 400 is non-fatal: derive path works.)
- Note: balance $424.55 < $500 Stage-1 target (fine for 0b/0c, ~$425 Stage 1).
- **Next (GATED — first real capital at risk):** launch Stage 0b — armed, budget_usd=50, ~1
  market, unattended. Awaiting operator "launch" before placing fillable live quotes.

### 2026-06-15 (cont.11) — shadow replay net vs fund size (31d, min_size sizing)
- Ran the replay across fund sizes (scan 150 mkts, 51 LP-friendly), net over ~31d:
  - $150→$970 ($940/mo, 626%) · $500→$1,429 ($1,385/mo, 277%) · $1k→$2,135 ($2,069/mo) ·
    $5k→$2,434 ($2,359/mo, 47%) · $25k→**only $6k deploys**→$2,515 ($2,437/mo, 41%).
- **Saturates ~$6k deployed / ~$2.4k/mo net** with this config; trading(inventory) drag
  grows with scale (−$57→−$1,339) as marginal/thin markets add inventory risk.
- **Reconciles & TEMPERS the cont.7 $11.5k/mo:** that allowed >min_size sizing (≤50% of
  book) and ignored inventory P&L; this enters at min_size only, scans 150/1000 mkts, and
  SUBTRACTS simulated inventory losses → ~$2.4k/mo. Truth between the two; levers = scan
  breadth, per-market size (more reward, more inventory risk), and real qScore capture
  (which cuts the thin-market over-capture inflating the $150 "626%/mo").
- **Goal read:** conservative min_size sizing lands **~$2–2.5k/mo net — BELOW the $5k bar**.
  Reaching $5k/mo needs broader coverage and/or larger per-market size (more risk), which
  only real Stage 0 can validate (these % are inflated by modelled over-capture). Updates
  best-candidate est to a conservative ~$2–2.5k/mo, optimistic case higher, unproven.

### 2026-06-15 (cont.10) — shadow LP bot complete: replay backtest + risk controls
- Extended `src/execution/lp_quoter.py` to a full SHADOW bot (still no client/money):
  `plan` (live snapshot of would-post orders) + `backtest` (31d hourly replay per
  selected market: re-quote to mid, sim fills on price-crossings, inventory skew at
  INV_CAP_MULT, JUMP_HALT, per-market kill-switch at -15% capital, reward accrual).
- $500 replay: 4 markets, net $928 over 31d ≈ **$899/mo (180%/mo) simulated**,
  decomposed **reward $1047 + trading −$120**. Reward is the engine; inventory/trading
  is the drag. Kill-switch fired on 2/4 markets; Eizenkot (VR 0.62) still lost −$134 on
  inventory ⇒ **VR<1 is necessary not sufficient** — an adverse run can sink a market.
- Headline stays optimistic: net dominated by one thin market where our order is ~80%
  of book (the over-capture real qScore competition will cut). The DELIVERABLE is the
  reward-vs-inventory decomposition + demonstrated risk controls, not the % .
- **Shadow is now as far as it usefully goes:** the live continuous loop's real value
  (actual fills/reward) is only observable armed. Remaining = wire the armed executor
  for Stage 0. GATED: needs V2 allowances + ~$150 USDC + arm.

### 2026-06-15 (cont.9) — shadow LP quoter built + running (free, no money)
- Built `src/execution/lp_quoter.py` (SHADOW core, no client/arming). Runs live
  selection (VR<1 + pool + min_size + hurdle filters) → quote math (buy-YES/buy-NO,
  tick-snapped) → $-budget block allocation → prints would-post orders + est reward.
- First $500 shadow run: 3 markets (Eizenkot next-Israeli-PM, Belgium Grp G, US-Cuba),
  $493 capital, est $192/mo (39%/mo) — labelled UNVERIFIED size-share proxy. Only 3
  because the top-40-by-pool scan skews to min_size 200; broadening to low-min_size
  markets (a free config sweep) spreads across the 6–10 events targeted.
- **Confirmed answer to "shadow vs real":** shadow proves the *machinery* (selection,
  quote math, sizing, allocation, risk plumbing) at zero cost — done. It CANNOT
  produce realized capture-rate, fills, or inventory P&L: those are on-chain physical
  events that require real orders. ⇒ tiny real **Stage 0 (~$150)** is unavoidable for
  the edge numbers, but shadow de-risks the whole build first.
- **Next (free, in-bounds):** finish the shadow bot — continuous re-quote loop on a
  moving mid, kill-switch/jump-halt, optional fill simulation, metrics logger. Then
  GATED Stage 0: V2 allowances + ~$150 USDC + arm executor.

### 2026-06-15 (cont.8) — LP live-pilot spec written (gated on operator go)
- Wrote [LP_PILOT_SPEC.md](LP_PILOT_SPEC.md): purpose (measure realized capture-rate,
  inventory/jump P&L, capacity — not profit), market filter (VR<1, pool≥$20, slow/no
  imminent event, min_size≤100), two-sided quoting via buy-YES+buy-NO bids (sidesteps
  PM buy-only), inventory skew (no Betfair hedge), kill-switch/jump-halt, metrics, and
  infra reuse (polymarket_executor V2 GTC + maker_executor skeleton; build delta = an
  "LP mode" module, ~1–2 days).
- **Minimum USDC:** recommend **$500** to start (Stage 0 plumbing ~$150 for 2–3 days →
  Stage 1 measurement $500 for ~2 weeks). Floor reasoning: ~$1/share two-sided,
  min_size 50–200 ⇒ $50–200/market; need ≥5–6 markets to separate signal from a single
  market's jump ⇒ ≥$300; $500 gives 6–10 markets + inventory buffer. Hard-capped.
- Prereqs flagged: USDC.e on Polygon in POLY_FUNDER, POL gas, **V2 allowances**
  (`scripts/set_v2_allowances.py`), confirm PM sell/NO-side under V2 (were buy-only),
  fill-polling latency acceptable. SHADOW first, then armed.
- **Next action (GATED):** operator funds + approves → build the LP quoting module →
  Stage 0 plumbing → Stage 1 measurement → decision gate (net ≥ ~40–50% of model gross
  → scale; else re-scope/kill).

### 2026-06-15 (cont.7) — H4 capacity CORRECTED: scales with breadth, not saturated
- Re-ran the allocator over ~all 120 in-band candidates (98 LP-friendly), prudent
  per-market cap = 50% of book. **The cont.6 "saturates fast" was a 40-market sampling
  artifact** — with the full universe profit keeps climbing with capital:
  - $500 → ~$3,150/mo (630%/mo) · $1k → ~$4,150 (415%) · $5k → ~$5,100 (102%) ·
    **$25k → ~$11,500/mo gross (46%/mo)**.
- **Mechanism:** LP capacity scales with **breadth** (spread across 98 markets), not
  depth in one (own-share dilution). Per-$ yield falls steeply but absolute $/mo rises.
- **Still GROSS & optimistic:** size-share overstates real qScore capture (which rewards
  tight-to-mid); 50%-of-book across ~100 thin markets = large unmodelled jump exposure;
  ~100-market continuous quoting is a real MM system. Apply ~40–60% haircut →
  **$25k plausibly NETS ~$4–6k/mo** ⇒ the $5k/mo bar is **reachable in principle, not
  proven**. This REVISES the cont.6 pessimism: capacity is likely NOT the blocker;
  realized capture-rate + jump P&L are the real unknowns.
- Best-candidate table updated: H4 est. $/mo @ $25k ≈ $4–6k modelled-net (was "unproven/
  capacity-bound"). Still gated on a live pilot to measure realized net.

### 2026-06-15 (cont.6) — H4 capacity: returns saturate fast (key constraint)
- Added a budget allocator to `lp_net_yield.py` (block-entry at `min_size`, then greedy
  marginal reward/$). On the 40-market sample:
  - **$500 → ~$27/day ≈ $800/mo gross (~160%/mo)** across 7 mkts (dominated by thin
    low-min_size pools: Texas Senate $20/d, Maine Gov races $10–30/d).
  - **$1,000 → ~$30/day ≈ $906/mo (+12% only)** — returns **saturate almost immediately**.
- **Implication for the $5k/mo goal:** the high % is an artifact of tiny capital
  capturing thin pools; capturable reward across the good markets looks like
  **low-thousands/mo total**, NOT linearly scalable to $25k. Capacity (the binding
  constraint in GOAL's recycle model) is the likely ceiling, and may fall **short of
  $5k/mo** unless the accessible good-market set is much larger than this sample.
- **Realistic $500 expectation** (after haircuts — qScore-vs-size-share capture ~40–60%,
  uptime/re-quote losses, unmodelled jump risk on thin books where you'd be 50–90% of
  liquidity): planning range **~$150–400/mo net**, wide CI, with tail risk of a losing
  week on a jump. The pilot's purpose = measure realised capture-rate + inventory/jump
  P&L to calibrate, and to map the true capacity ceiling — not the $500 return itself.

### 2026-06-15 (cont.5) — H4 NET-yield screen: mean-reversion confirms LP-friendliness
- Built `analysis/lp_net_yield.py`. Per market: book competition (existing balanced
  qualifying shares within max_spread) + `/prices-history` (hourly, 31d) → realised
  daily vol σ + **variance ratio VR** (6h/1h). Model: capital ≈ $S for S shares 2-sided;
  small-maker reward ≈ 30·pool/qual_shares /mo; adverse haircut applied only when VR>1
  (trending=informed); VR≤1 (mean-reverting) ⇒ trading P&L≥0 ⇒ reward ≈ net. SCREEN,
  not a measured net.
- **Result (20 slow markets, $10–600/day pools):** **17/20 mean-reverting (VR<1)** —
  confirms slow prediction markets are noise-trader-dominated and LP-friendly. Trending
  markets behaved correctly: Brazil WC (VR 1.56) net −2.8%, S.Korea Grp A (VR 1.72)
  net −48%. Net %/mo: median 10.6 among LP-friendly; range 1–30% on *scalable* mkts
  (Bennett-next-PM 26%, Texas Senate 31%), 130–190% on *thin* dust (Maine Gov, nominee).
- **Honest capacity caveat:** the eye-popping % is concentrated in NON-scalable dust —
  ~$22 of the $37/day sample capture came from 2 tiny-pool markets where $200 grabs
  60–90% of a $10–20/day pool. Scalable liquid pools give modest per-market $ and your
  share shrinks as you add capital. So the linear extrapolation (~$6–8k/mo @ ~$24k) is
  **optimistic**; true realizable $/mo at $25k is unproven and capacity-bound.
- **Other caveats:** size-share is a generous qScore proxy (ignores closeness-to-mid
  competition); VR<1 can be partly bid-ask bounce (overstates friendliness); model
  omits news/resolution JUMP risk (resting orders run over on a goal/dropout); maker
  fees/gas small but nonzero. Net is modelled, not realised.
- **Verdict:** H4 is the **clear #1 and the first candidate with a credible positive
  net mechanism** — direction is solid (LP-friendly flow, reward >> modelled adverse
  cost on slow markets). What it can't settle on paper: **realised net + true capacity
  to $5k/mo**. That requires placing real resting orders and measuring reward-earned vs
  inventory P&L over days = **live, real-USDC quoting = GATED**.
- **STOP — operator gate:** further validation needs a funded live pilot. Recommend a
  small capped paper→live LP pilot (e.g. $500–1,000 across 5–10 mean-reverting slow
  markets: Bennett PM, Texas/various Senate, WC group-winners, long-dated elections)
  to measure realised reward vs inventory P&L for ~1–2 weeks before scaling. Needs sign-off.

### 2026-06-15 (cont.4) — H4 Polymarket LP: first measurements (operator chose H4)
- **Gross pool:** `clob.polymarket.com/sampling-markets` → 1000 reward markets (paged,
  more exist), **$17,104/day ≈ $513k/mo** total `rewards_daily_rate`. Params per
  market: `min_size` (qualifying order sz), `max_spread` (¢ from mid to qualify).
  Concentrated in slow long-dated markets: WC winners (France/Spain $2,820/day each),
  elections — ideal LP (price barely moves intraday → low adverse selection).
- **Competition (top market, France WC):** book is 0.16/0.161 (0.1¢), **~$6.8M
  qualifying notional** already competing for $2,820/day ⇒ gross ≈ **16%/mo** on
  capital; posting a few hundred $ earns ~$0. Headline markets are **farmed**.
- **Yield/$ distribution (24-market sample across the rate spectrum):** wildly
  dispersed — **median ~19%/mo gross**, p25 1.3%, p75 77%, plus a tail of thin
  under-quoted markets at 300–470%/mo and several with **$0 qualifying liquidity**
  (nobody quoting). Sweet spot = slow markets w/ moderate pools (e.g. Uruguay Group H
  $45/day, 32%/mo gross). The big pools are efficient; the edge is in the under-farmed tail.
- **Caveats (do not overclaim):** all figures **GROSS, pre-adverse-selection**;
  high-% markets have tiny $ pools ($1–3/day) and are thin/fast (likely *why*
  unfarmed — inventory risk eats reward); qScore weights closeness-to-mid &
  2-sidedness so size-share is only an order-of-magnitude proxy; books are a single
  snapshot. NET yield is unproven.
- **Verdict:** H4 open→**testing, #1 candidate** — best evidence in the hunt. Gross
  15–30%/mo on slow markets ⇒ even a heavy net haircut could approach the $5k/mo bar
  at $25k. Clears the "is there anything here" screen; does NOT yet clear the GOAL bar
  (needs net yield + drawdown + capacity + a paper record).
- **Next action:** model **NET** yield on ~10 slow, moderate-pool target markets:
  qScore share × daily pool − adverse-selection cost estimated from PM price-move
  history (resolution-horizon vol) − capital-dilution from your own posted size; then
  scope capacity (Σ deployable across the tail before yields compress to $25k). NB:
  *collecting* rewards for real = live resting orders on-chain = **GATED** (operator
  sign-off); modelling stays paper. Tooling: reuse `src/discovery/polymarket.py` +
  CLOB `/book`, `/prices-history`.

### 2026-06-15 (cont.3) — Shin de-vig kills H2-value; "beat the sharp" thesis exhausted
- Re-ran `arb_scan.py` with **Shin** de-vig (corrects favourite-longshot bias;
  `DEVIG=shin`, now default) vs the prior multiplicative method.
- Longshot-band edge **halved** (+9.6%→+4.6%). Credible liquid subset (matched >£50k,
  odds<6, size≥£20) collapsed to **+2.7% / +1.2% / +0.5%**. The only selection with
  real depth (Brazil v Haiti HOME, **£31k** available) had **+0.5%** edge ≈ zero.
- **Total honest deployable +EV on a peak 60-fixture World Cup day ≈ £20** — inside
  de-vig-method noise, before VWAP haircut. **H2-value = KILLED.** On liquid markets
  Betfair is as sharp as Pinnacle; there is no systematic value between two sharp
  venues. (Top-of-book-only also overstates this, so the real number is ≤ £20.)
- **Strategic read:** the entire **"beat the sharp closing/consensus line" family is
  now exhausted** — soft-book value (H1) didn't survive removing look-ahead; exchange
  value (H2) is efficient. This was scope area #1 (PRIMARY). Riskless back/lay **arb**
  (H2 pure form) remains technically untested but the existing baseline already pegs
  it at ~$20/mo, depth-bound — unlikely to reach $5k/mo alone.
- **Decision point (handing back):** pivot the hunt to the **non-sharp-line** tracks:
  **H4 Polymarket LP / liquidity-reward yield** (market-neutral, no gubbing, no sports
  seasonality, flagged "untapped" in GOAL) is the strongest untested candidate; H5
  crypto is the tertiary fallback. Recommend H4 next.

### 2026-06-15 (cont.) — OddsPapi access = Pinnacle-only; architecture pivot
- Probed book access on the odds endpoint. **Restricted on our plan:** `betfair-ex`
  AND every soft book tried (`bet365, 1xbet, williamhill, unibet, betano, betway,
  bwin, 888sport, betsson`) → `403 RESTRICTED_ACCESS`. **Accessible: `pinnacle`**
  (200, full payload incl. `limit`). Treat OddsPapi on this plan as a **Pinnacle
  sharp-line feed**, not a multi-book aggregator.
- (Cloudflare note: urllib needs a `User-Agent` header or you get `403 err 1010`;
  curl-style UA fixes it. Codified in `analysis/arb_scan.py`.)
- **Consequence — this is GOOD for the guardrails.** The only viable cross-venue
  pair from our data is **OddsPapi-Pinnacle ↔ own-Betfair-Exchange** — both
  **non-gubbing** (Pinnacle won't ban; Betfair is an exchange, commission not bans).
  Soft-book restriction removes the gubbing-prone leg by force. H2 becomes
  specifically: *does Betfair's exchange back/lay dislocate from Pinnacle's de-vigged
  fair, at deployable size, point-in-time?* — and we already have the Betfair client
  + event-matcher to build it.
- **Next action (revised):** build `arb_scan.py` v2 = pull Pinnacle 1X2 (+`limit`) for
  upcoming fixtures via OddsPapi, pull the matched Betfair market via our own client,
  align outcomes, and compute (a) |Betfair_mid − Pinnacle_fair| dislocation
  distribution, (b) any back/lay > Pinnacle_fair (value vs sharp) and its size, capped
  by Pinnacle `limit` ∧ Betfair depth. NB seasonality: in summer, run it on whatever
  Pinnacle prices (intl/World Cup, S. American leagues), accept lower volume.

### 2026-06-15 — OddsPapi live recon for H2 (data-access reality check)
- Confirmed live: `/v4/sports`, `/v4/tournaments?sportId=`, `/v4/bookmakers` (371
  books), `/v4/odds-by-tournaments?tournamentIds=&bookmaker=`. Pinnacle, Betfair-ex,
  Matchbook all *appear* in `/v4/bookmakers`.
- **Constraint 1 (big):** `betfair-ex` (and exchanges) are **RESTRICTED on our plan**
  — `403 RESTRICTED_ACCESS, "You do not have access"`. So the **exchange lay leg of
  any arb cannot come from OddsPapi**; it must come from our **own Betfair API**
  (which we have). H2 therefore = OddsPapi(Pinnacle+soft) **joined to** own-Betfair
  feed via the existing event-matcher — a real build, not a one-call measurement.
- **Constraint 2:** odds endpoint rate-limits at **~1 req/s** (`429`, ~0.9s wait).
  Quota-disciplined fan-out needed (≤1 tournament/sec).
- **Constraint 3:** mid-June = top-5 Euro leagues OFF-SEASON (EPL/LaLiga/Bundesliga/
  Ligue1 `futureFixtures=0`). Live coverage = minor leagues (Brazil, Norway, friendlies);
  Pinnacle does **not** price many of them (Brazil Série A id 325 → no Pinnacle fixtures).
  Seasonality will throttle deployable volume in summer — relevant to $/mo capacity.
- **Win:** Pinnacle odds payload includes a per-outcome **`limit`** field (e.g. 625 /
  1038 / 3125) = the max stake accepted at that price = a **direct depth measurement**
  for the recycle model, plus `changedAt` timestamps for point-in-time/line-move work.
- Status: H2 open→**testing (partially blocked)**. No EV number yet — gated on joining
  the own-Betfair feed for the exchange leg.
- **Next action:** build a small `analysis/arb_scan.py` that (a) pulls Pinnacle+soft
  for the live/upcoming tournaments via OddsPapi (≤1 req/s), (b) pulls the matching
  Betfair-exchange market via our own client, (c) matches events (reuse matcher), and
  (d) computes back-vs-lay arb % and the size it absorbs (capped by Pinnacle `limit` ∧
  Betfair available depth). First real **deployable-edge/day** read on a non-gubbing pair.
