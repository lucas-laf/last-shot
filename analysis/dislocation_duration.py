"""How long do tennis lock-arb dislocations actually stay open?

Reconstructs, from recorded ticks, every window where a lock-arb edge existed
on a matched tennis pair (same engine formula + fee model + min-notional gate as
live), and measures each window's DURATION. The point: decide whether speeding
up the taker can recapture missed tennis arbs, or whether the windows are simply
shorter than our ~94ms PM order round-trip (in which case no realistic speed
work helps — they're physically uncatchable from one box).

A window is "catchable" if it stays open longer than our react-and-place
latency: feed propagation (~ms, co-located) + eval (~us) + PM order RTT
(~94ms median, 217ms p90). We bucket against those lines.

Usage: python -m analysis.dislocation_duration [--category tennis]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.models import MatchStatus, Platform  # noqa: E402
from src.settings import load_settings  # noqa: E402
from src.signals import fees  # noqa: E402
from src.storage import Store  # noqa: E402

TICK_DIR = Path("data/ticks")


def tick_files() -> list[str]:
    """Complete hourly parquet files only — skip the in-progress current hour
    (a being-written file reports size 0 and breaks duckdb prefetch)."""
    files = sorted(p for p in TICK_DIR.glob("ticks_*.parquet")
                   if p.stat().st_size > 0)
    return [str(p) for p in files[:-1]] if len(files) > 1 else [str(p) for p in files]
# Capture-latency reference lines (ms): our measured PM order round-trip.
PM_RTT_MEDIAN_MS = 94.0
PM_RTT_P90_MS = 217.0
BUCKETS_MS = [0, 25, 50, 94, 100, 250, 500, 1000, 5000, float("inf")]


def tennis_pairs(store: Store, category: str):
    pairs = store.get_pairs([MatchStatus.AUTO_ACCEPTED, MatchStatus.APPROVED])
    bf_markets = {m.market_id: m for m in store.get_markets(Platform.BETFAIR, active_only=False)}
    pm_markets = {m.market_id: m for m in store.get_markets(Platform.POLYMARKET, active_only=False)}
    out = []
    for p in pairs:
        bf = bf_markets.get(p.betfair_market_id)
        if not bf or bf.category != category or not p.verdict.outcome_mapping:
            continue
        link = p.verdict.outcome_mapping[0]
        pm = pm_markets.get(p.polymarket_market_id)
        out.append((p.betfair_market_id, link.betfair_selection_id,
                    link.polymarket_token_id, pm.taker_fee if pm else 0.0, link.name))
    return out


def windows_for_pair(con, files, bf_mid, bf_sel, pm_tok, r, c, min_edge,
                     min_pm_notional, min_bf_stake):
    """Return list of (duration_ms, peak_edge) for every continuous open window."""
    rows = con.execute(
        """select epoch_ms(ts) as t_ms, platform, bid, ask, bid_size, ask_size
            from read_parquet(?)
            where (platform='betfair' and market_id=? and outcome_id=?)
               or (platform='polymarket' and outcome_id=?)
            order by ts""",
        [files, bf_mid, bf_sel, pm_tok]).fetchall()

    bf_q = pm_q = None
    open_ts = None
    peak = 0.0
    out = []
    for t_ms, plat, bid, ask, bsz, asz in rows:
        if plat == "betfair":
            bf_q = (bid, ask, bsz, asz)
        else:
            pm_q = (bid, ask, bsz, asz)
        if not bf_q or not pm_q:
            continue
        bfb, bfa, bfbsz, bfasz = bf_q
        pmb, pma, pmbsz, pmasz = pm_q
        open_now = False
        edge = 0.0
        if bfb > 0 and pmb > 0 and bfa < 1 and pma < 1:
            # dir1: buy PM @ ask, lay Betfair @ bid (consumes back-side £ at bid)
            e1 = fees.betfair_sell(bfb, c) - fees.polymarket_buy(pma, r)
            d1 = (e1 >= min_edge and (pmasz or 0) * pma >= min_pm_notional
                  and (bfbsz or 0) >= min_bf_stake)
            # dir2: sell PM @ bid, back Betfair @ ask (consumes lay-side £ at ask)
            e2 = fees.polymarket_sell(pmb, r) - fees.betfair_buy(bfa, c)
            d2 = (e2 >= min_edge and (pmbsz or 0) * pmb >= min_pm_notional
                  and (bfasz or 0) >= min_bf_stake)
            open_now = d1 or d2
            edge = max(e1 if d1 else 0.0, e2 if d2 else 0.0)
        if open_now:
            if open_ts is None:
                open_ts = t_ms
                peak = edge
            else:
                peak = max(peak, edge)
        elif open_ts is not None:
            out.append((float(t_ms - open_ts), peak))
            open_ts = None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="tennis")
    ap.add_argument("--min-edge", type=float, default=None,
                    help="override engine min_edge (default: live env value)")
    ap.add_argument("--commission", type=float, default=None,
                    help="override Betfair commission (default: live env value)")
    args = ap.parse_args()

    cfg = load_settings()
    store = Store(cfg["data_dir"])
    c = args.commission if args.commission is not None else cfg["signals"]["betfair_commission"]
    min_edge = args.min_edge if args.min_edge is not None else cfg["signals"]["min_edge"]
    min_pm_notional = cfg["execution"].get("min_pm_notional", 1.0)
    min_bf_stake = cfg["execution"].get("min_bf_stake_gbp", 2.0)

    pairs = tennis_pairs(store, args.category)
    con = duckdb.connect()
    files = tick_files()

    all_w = []
    pairs_with = 0
    for bf_mid, bf_sel, pm_tok, r, name in pairs:
        w = windows_for_pair(con, files, bf_mid, bf_sel, pm_tok, r, c, min_edge,
                             min_pm_notional, min_bf_stake)
        if w:
            pairs_with += 1
            all_w.extend(w)

    print(f"category={args.category}  matched pairs={len(pairs)}  "
          f"pairs with >=1 dislocation={pairs_with}")
    print(f"min_edge={min_edge}  commission={c}  "
          f"min_pm_notional=${min_pm_notional}  min_bf_stake=£{min_bf_stake}")
    print(f"total dislocation windows: {len(all_w)}\n")
    if not all_w:
        print("No windows — no overlapping tennis ticks with a takeable edge.")
        return

    durs = sorted(d for d, _ in all_w)
    n = len(durs)

    def pct(thresh):
        k = sum(1 for d in durs if d >= thresh)
        return k, 100.0 * k / n

    print("=== window-duration distribution (count of opportunities) ===")
    labels = ["<25ms", "25-50ms", "50-94ms", "94-100ms", "100-250ms",
              "250-500ms", "500ms-1s", "1-5s", ">5s"]
    for i, lab in enumerate(labels):
        lo, hi = BUCKETS_MS[i], BUCKETS_MS[i + 1]
        k = sum(1 for d in durs if lo <= d < hi)
        bar = "#" * int(40 * k / n)
        print(f"  {lab:11s} {k:5d}  {100.0*k/n:5.1f}%  {bar}")

    print("\n=== catchability (windows lasting >= latency line) ===")
    for thr, note in [(PM_RTT_MEDIAN_MS, "PM RTT median 94ms"),
                      (PM_RTT_P90_MS, "PM RTT p90 217ms"),
                      (500.0, "comfortable 500ms"),
                      (1000.0, "easy 1s")]:
        k, p = pct(thr)
        print(f"  >= {thr:6.0f}ms ({note:20s}): {k:5d} / {n}  = {p:5.1f}% catchable")

    med = durs[n // 2]
    print(f"\nmedian window = {med:.0f}ms   "
          f"p25={durs[n//4]:.0f}ms  p75={durs[3*n//4]:.0f}ms  "
          f"max={durs[-1]:.0f}ms")
    # the headline: fraction physically uncatchable (shorter than our place RTT)
    _, sub = pct(PM_RTT_MEDIAN_MS)
    print(f"\n>>> {100-sub:.0f}% of tennis dislocations close in under our 94ms "
          f"PM round-trip — uncatchable from one box no matter how we tune.")
    print(f">>> {sub:.0f}% last long enough to be catchable if latency were the "
          f"only barrier.")


if __name__ == "__main__":
    main()
