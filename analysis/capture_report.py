"""Shadow capture-rate report: `python -m analysis.capture_report`.

Reads shadow_orders (written by the live ArbExecutor) and answers: of the
lock_arb quotes we decided to hit, what fraction would still have been there
when our order arrived, given measured venue RTTs? Split by category,
platform and RTT bucket — the empirical input for the go/no-go on tennis.

Caveat printed with every run: 'timeout_unchanged' rows assume an unchanged
book means a live quote; Polymarket cancels invisible between WS updates make
PM capture rates an upper bound.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    db = PROJECT_ROOT / "data" / "lastshot.db"
    if not db.exists():
        sys.exit("no data/lastshot.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    rows = con.execute("""
        SELECT coalesce(m.category, '?'), s.platform, s.rtt_ms, s.captured,
               s.basis, s.decide_us
        FROM shadow_orders s
        LEFT JOIN markets m ON m.platform='betfair'
                           AND m.market_id = s.betfair_market_id
        WHERE s.captured IS NOT NULL
    """).fetchall()
    if not rows:
        sys.exit("no resolved shadow orders yet — let the tracker run")

    print(f"=== Shadow capture rates ({len(rows)} resolved orders) ===")
    groups = defaultdict(lambda: [0, 0])
    for cat, platform, rtt, captured, basis, _ in rows:
        for key in ((cat, platform), (cat, "both"), ("ALL", platform)):
            g = groups[key]
            g[0] += 1
            g[1] += captured
    print(f"{'category':12s} {'platform':11s} {'n':>6s} {'captured':>9s}")
    for (cat, platform), (n, c) in sorted(groups.items()):
        print(f"{cat:12s} {platform:11s} {n:>6d} {100*c/n:>8.1f}%")

    print("\n=== by basis ===")
    basis_g = defaultdict(lambda: [0, 0])
    for _, _, _, captured, basis, _ in rows:
        basis_g[basis][0] += 1
        basis_g[basis][1] += captured
    for basis, (n, c) in sorted(basis_g.items()):
        print(f"  {basis:20s} n={n:<6d} captured {100*c/n:.1f}%")

    decide = sorted(d for *_, d in rows if d is not None)
    if decide:
        q = lambda p: decide[int(p * len(decide))]  # noqa: E731
        print(f"\ndecision latency (signal->order-ready): median {q(0.5):.0f}us"
              f"  p90 {q(0.9):.0f}us  p99 {q(0.99):.0f}us")
    print("\nNote: PM rates are an upper bound (cancels invisible between WS updates).")


if __name__ == "__main__":
    main()
