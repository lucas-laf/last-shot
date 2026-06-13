# last_shot — project status & handoff



## What this is

A cross-platform sports/politics arbitrage system between **Betfair** (UK
betting exchange, live API key) and **Polymarket** (US-hosted prediction
market, on-chain USDC). It discovers equivalent markets on both venues,
matches them (LLM-assisted, human-reviewed), tracks them in real time, and
detects price dislocations. Currently **paper-trading only** plus a **shadow
executor** measuring real fill feasibility. No real orders have been placed.

Repo: https://github.com/lucas-laf/last-shot
Live box: EC2 `i-068384d6dcb71bee2`, t3.medium, eu-west-1 (Dublin), systemd
`lastshot-tracker`. Laptop runs the orchestration loop + paper trading.

## Strategies evaluated

| Strategy | Verdict | Evidence |
|---|---|---|
| **lock_arb** (buy one venue, sell the other, hedged to settlement) | **KEEP — the real edge** | 184 settled events, 97 profitable; realized +1.20% vs locked +1.74% ROI on $165k cycled capital; event-level mean +1.72%/share, **t = 1.90**. Realized tracks the locked edge — signature of genuine arbitrage. Zero cross-platform resolution disagreements to date. |
| **convergence** (bet the platform diverging from a "true" reference) | **RETIRED 2026-06-12** | No reference policy (deeper/betfair/polymarket) survived out-of-sample. The spread-filter carve-out looked great in-sample (+7.7%/share, t=3.3) and collapsed out-of-sample (-1.3%/share, t≈-0.6, ~86 events). Disabled in live engine (`convergence_enabled: false`); still reconstructable from ticks via `analysis/replay.py`. |
| **maker-side lock arb** (rest passive quotes inside PM spread, hedge on Betfair) | **PROMISING — build next** | Backtest: ~$150-220/day locked at $100 caps **if** cancel/replace latency <0.5s (we have ~50-150ms from Dublin). No capture-rate race since you're filled by definition. See `analysis/maker_sniper_backtest.py`. |
| **post-result sniping** (buy known winner on PM after Betfair suspends) | **DEAD** | Backtest found ~1 real opportunity ever; PM market makers pull quotes at the result. |

## Key empirical facts (don't re-derive)

- **Edge is a settlement edge, not a scalping edge**: take-profit/cash-out rules
  all underperform hold-to-settlement (they cap winners, keep losers).
- **Bigger apparent edge = worse, not better**: large divergences are stale/wide
  quotes, not free money. A profitable arb band's "5% ROI" was 90% directional
  luck on unpaired legs; the locked edge was 0.48%.
- **Profits land on the Polymarket side** (PM is the less efficient venue). The
  Betfair leg runs ≈breakeven → **Betfair Premium Charge never triggers** (that
  account never shows lifetime profit).
- **Realistic economics**: ~1.5% locked ROI per deployment cycle, same-day
  capital recycling on sports. ~165 arbs/day, median executable size ~$68/arb
  (44% clipped by the $100 paper cap, so real depth is larger; PM profitable-band
  depth runs 1k-11k shares).
- **Costs are negligible vs edge**: FX/rebalancing ~0.3-0.5% of converted flow,
  AWS ~$20-40/mo, leg-risk ~0.05-0.1% of volume. Break-even capture rate <2%.
- **Latency (warm, from Dublin)**: Betfair ~17ms, Polymarket CLOB ~19ms. Order
  build+sign ~75ms. us-east-1 was *worse* for PM CLOB (94ms) → single Dublin box
  is correct, no split-region needed.
- **Capture (shadow, no money)**: 2,275 resolved shadow orders, ~99.6% "captured"
  — but dominated by slow politics/soccer windows and limited by sparse PM tick
  data. **The real tennis capture rate (1.3s windows) is still unmeasured** and is
  the key unknown; only live small-stakes testing answers it.

## Infrastructure state

- **Tracker + shadow executor**: live on EC2 (systemd, auto-restart, WAL DB) and
  laptop. Both feeds healthy. Hourly orchestration loop (discovery → LLM/Claude
  match review → ingest → settle → git) runs from the laptop.
- **Execution module** (`src/execution/`): built and wired, **disarmed**.
  `ArbExecutor` sizes both legs to min(displayed depth, cap); safety rails =
  ARMED=false default + `EXECUTOR_ARMED` env gate, per-outcome cap, daily capital
  cap, soccer/politics whitelist to start.
- **Polymarket auth + V2**: fully verified end-to-end. Polymarket migrated to
  **CLOB V2** (2026-04-28); we now run `py-clob-client-v2`. The funded wallet is
  a **POLY_1271 / deposit-wallet (signature_type 3)** — only sig_type 3 returns
  the on-chain balance from the CLOB. $117.98 held as **pUSD** (the V2 collateral
  token, `0xC011…`) in `0x9bdb…a4c3`; V2 exchange allowances already unlimited
  on-chain. A live order was placed + cancelled successfully (no `invalid order
  version`). Balance/positions readable via API.
- **AWS budgets**: $20/day + $40/month, email alerts. Run rate ~$0.60/day.
- **VNC + Firefox** on the box for manual Polymarket access (tunnel :5901).



## Next steps when resuming (in order)

1. ~~**Fix the Polymarket order-builder for neg-risk markets**~~ — **DONE
   2026-06-13.** The `invalid order version` error was misdiagnosed: it was the
   **CLOB V2 migration**, not neg-risk. Migrated to `py-clob-client-v2`, fixed a
   separate tick-size snapping bug (prices must sit on the 0.01/0.001 grid, not a
   hardcoded 3-decimal round), and corrected signature_type 2→3. Verified with a
   live placed-and-cancelled order. Test harness: `scripts/place_test_order.py`
   (dry-run default, `--arm` to send). Allowance helper: `scripts/set_v2_allowances.py`
   (not needed — V2 approvals already set on-chain).
2. **Arm on soccer/politics only**, minimum stakes (£2/$3 legs), with the existing
   caps scaled to the ~$100 float. Add a min-notional filter so the executor skips
   arbs the float can't cover (Betfair £2 min stake).
3. **Add both-legs-or-unwind handling** (Phase B): fire PM leg first (the stale
   side), hedge on Betfair; auto-unwind if only one leg fills. This is the real
   leg-risk safety and must exist before tennis.
4. **Measure real tennis capture rate** at min stakes for ~1 week — the number
   that decides whether the bulk of the opportunity (fast tennis windows) is
   actually harvestable. Compare against the shadow curve.
5. **Build maker-side mode** (`analysis/maker_sniper_backtest.py` is the spec):
   the higher-capacity, latency-tolerant path; likely the real business if taker
   capture disappoints.
6. **Add a per-outcome trade cap** to the paper trader regardless — the cooldown
   re-fires up to ~100x on one outcome, which distorted convergence dollar P&L and
   would concentrate live risk.

## Useful commands

```bash
# health / numbers
ssh -i ~/.ssh/lastshot.pem ubuntu@<ec2-ip> "systemctl status lastshot-tracker"
.venv/bin/python -m analysis.edge_report          # realized PnL by signal/category
.venv/bin/python -m analysis.replay               # convergence counterfactuals
.venv/bin/python -m analysis.capture_report        # shadow capture rates
.venv/bin/python -m analysis.maker_sniper_backtest # maker/sniper backtests

# EC2 IP changes on stop/start:
aws ec2 describe-instances --region eu-west-1 --instance-ids i-068384d6dcb71bee2 \
  --query "Reservations[0].Instances[0].PublicIpAddress" --output text
```

