"""Backtests for two candidate strategies, from recorded ticks:
`python -m analysis.maker_sniper_backtest`

MAKER: rest passive limit orders inside the Polymarket spread, priced so an
instant Betfair hedge locks >= margin (PM charges takers only, so our side is
fee-free; the hedge pays Betfair commission). Fill proxy: a later PM tick
whose opposite touch crosses our level — a lower bound on real fills, biased
toward the most adversely-selected ones, so hedge slippage is honestly
represented: the hedge executes at the Betfair book AS OF the filling tick.

SNIPER: after the last Betfair tick of a resolved event (the exchange
suspends at the result), any remaining Polymarket quote that disagrees with
the known outcome is near-free money. Conservative: only ticks >30s after
Betfair's last print, one take per event, taker fees paid.
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from analysis.replay import build_states, build_streams, load_results, load_ticks  # noqa: E402
from src.models import Platform  # noqa: E402
from src.settings import load_settings  # noqa: E402
from src.signals import fees  # noqa: E402
from src.storage import Store  # noqa: E402

FRESH_S = 30.0
CAP = 100.0  # shares per fill/take, matching the paper cap


def merged(bf_stream, pm_stream):
    ev = [(t, "bf", b, a, bs, As) for t, b, a, bs, As in zip(*bf_stream)] + \
         [(t, "pm", b, a, bs, As) for t, b, a, bs, As in zip(*pm_stream)]
    ev.sort(key=lambda x: x[0])
    return ev


def maker_backtest(states, streams6, commission, margin, cats, refresh_s=0.0):
    """Returns per-category [fills, locked_pnl, unhedgeable] plus fill list.

    refresh_s models cancel/replace latency: quotes can only be updated every
    refresh_s seconds, so a Betfair move inside that window picks off the
    stale quote — the real adverse-selection cost of making."""
    out = defaultdict(lambda: [0, 0.0, 0])
    fills = []
    for s in states:
        bf = streams6.get(("betfair", s.betfair_market_id, s.betfair_selection_id))
        pm = streams6.get(("polymarket", s.polymarket_market_id, s.polymarket_token_id))
        if not bf or not pm:
            continue
        cat = cats.get(s.betfair_market_id, "?")
        c = commission
        bfq = pmq = None
        bft = 0.0
        q_buy = q_sell = None
        cool_buy = cool_sell = 0.0
        last_refresh = -1e18
        for t, src, b, a, bs, As in merged(bf, pm):
            if src == "bf":
                bfq, bft = (b, a, bs, As), t
            else:
                pmq = (b, a, bs, As)
                # fills are only observable on PM ticks
                if q_buy and a <= q_buy + 1e-9 and t >= cool_buy:
                    if t - bft <= FRESH_S and bfq:
                        locked = fees.betfair_sell(bfq[0], c) - q_buy
                        size = min(bfq[2], CAP)
                        out[cat][0] += 1
                        out[cat][1] += locked * size
                        fills.append((cat, locked))
                    else:
                        out[cat][2] += 1
                    q_buy, cool_buy = None, t + 120
                if q_sell and b >= q_sell - 1e-9 and t >= cool_sell:
                    if t - bft <= FRESH_S and bfq:
                        locked = q_sell - fees.betfair_buy(bfq[1], c)
                        size = min(bfq[3], CAP)
                        out[cat][0] += 1
                        out[cat][1] += locked * size
                        fills.append((cat, locked))
                    else:
                        out[cat][2] += 1
                    q_sell, cool_sell = None, t + 120
            if not bfq or not pmq:
                continue
            # refresh quotes off the live Betfair hedge price — but only as
            # fast as our cancel/replace latency allows
            if t - last_refresh < refresh_s:
                continue
            last_refresh = t
            tgt_b = math.floor((fees.betfair_sell(bfq[0], c) - margin) * 100) / 100
            q_buy_new = tgt_b if (t >= cool_buy and pmq[0] < tgt_b < pmq[1]) else None
            tgt_s = math.ceil((fees.betfair_buy(bfq[1], c) + margin) * 100) / 100
            q_sell_new = tgt_s if (t >= cool_sell and pmq[0] < tgt_s < pmq[1]) else None
            q_buy, q_sell = q_buy_new, q_sell_new
    return out, fills


def sniper_backtest(states, streams6, results, taker_rates, buffer_s=30.0,
                    threshold=0.97):
    """One take per resolved event after Betfair's last print."""
    takes = []
    for s in states:
        won = results.get((s.betfair_market_id, s.polymarket_market_id))
        if won is None:
            continue
        bf = streams6.get(("betfair", s.betfair_market_id, s.betfair_selection_id))
        pm = streams6.get(("polymarket", s.polymarket_market_id, s.polymarket_token_id))
        if not bf or not pm:
            continue
        t_close = bf[0][-1]
        r = taker_rates.get(s.polymarket_market_id, 0.0)
        for t, b, a, bs, As in zip(*pm):
            if t <= t_close + buffer_s:
                continue
            if won and 0 < a < threshold and As > 0:
                profit = 1.0 - fees.polymarket_buy(a, r)
                takes.append((s.outcome_name, t - t_close, a, min(As, CAP), profit))
                break
            if not won and b > (1 - threshold) and bs > 0:
                profit = fees.polymarket_sell(b, r)
                takes.append((s.outcome_name, t - t_close, b, min(bs, CAP), profit))
                break
    return takes


def main() -> None:
    cfg = load_settings()
    store = Store(cfg["data_dir"])
    states = build_states(store)
    ticks = load_ticks(None, None)
    # streams with sizes (build_streams drops them)
    raw = defaultdict(list)
    for e, platform, market_id, outcome_id, bid, ask, bs, As in ticks:
        raw[(platform, market_id, outcome_id)].append((e, bid, ask, bs, As))
    streams6 = {k: tuple(zip(*v)) for k, v in raw.items()}
    cats = {m.market_id: m.category
            for m in store.get_markets(Platform.BETFAIR, active_only=False)}
    taker = {m.market_id: m.taker_fee
             for m in store.get_markets(Platform.POLYMARKET, active_only=False)}
    bf_result, _ = load_results(store, states, False)
    c = cfg["signals"]["betfair_commission"]
    days = (max(t[0] for t in ticks) - min(t[0] for t in ticks)) / 86400

    print(f"=== MAKER (rest inside PM spread, hedge on Betfair) — {days:.1f} days ===")
    for margin in (0.005, 0.01, 0.02):
        for refresh in (0.0, 0.5, 2.0):
            out, fills = maker_backtest(states, streams6, c, margin, cats,
                                        refresh_s=refresh)
            tot_f = sum(v[0] for v in out.values())
            tot_p = sum(v[1] for v in out.values())
            tot_u = sum(v[2] for v in out.values())
            neg = sum(1 for _, l in fills if l < 0)
            neg_p = sum(l * CAP for _, l in fills if l < 0)
            print(f"margin {margin:.3f} refresh {refresh:>3.1f}s: {tot_f:4d} fills"
                  f" ({tot_f/days:3.0f}/day), locked {tot_p:+8,.0f}"
                  f" (${tot_p/days:5,.0f}/day), picked off {neg:3d} for {neg_p:+,.0f},"
                  f" unhedgeable {tot_u}")
        print()

    print("\n=== SNIPER (post-result Polymarket takes) ===")
    takes = sniper_backtest(states, streams6, bf_result, taker)
    if not takes:
        print("no opportunities found")
        return
    profit = sum(sz * p for _, _, _, sz, p in takes)
    print(f"{len(takes)} resolved events offered a take "
          f"(threshold 0.97, >30s after BF close, one per event)")
    print(f"total profit: {profit:+,.2f}  (${profit/days:,.0f}/day)  "
          f"avg {profit/len(takes):+.2f}/event")
    lat = sorted(t for _, t, *_ in takes)
    print(f"window after BF close: median {lat[len(lat)//2]/60:.1f}min, "
          f"p10 {lat[len(lat)//10]/60:.1f}min")
    best = sorted(takes, key=lambda x: -(x[3] * x[4]))[:5]
    for name, dt, px, sz, p in best:
        print(f"   {name[:28]:28s} {dt/60:6.1f}min after close, px {px:.2f}, "
              f"size {sz:.0f}, profit {sz*p:+.2f}")


if __name__ == "__main__":
    main()
