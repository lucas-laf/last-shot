"""Live capture-rate report: `python -m analysis.live_capture_report`.

The live analog of capture_report.py. Reads live_trades (written by the armed
ArbExecutor) and reports, per category: the **lock rate** (both legs filled =
the live analog of shadow "captured"), the abort rate (PM killed) and unwind
rate (naked PM flattened), realized-vs-intended edge slippage, mean unwind cost,
and the PM-fill->Betfair-hedge latency window. It then prints the live lock rate
side-by-side with the shadow capture rate for the same category — the headline
go/no-go comparison.

Caveat printed every run: early on (and after a one-shot run) n is tiny, so this
is a smoke test, not a statistic.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _shadow_capture_by_cat(con: sqlite3.Connection) -> dict[str, float]:
    rows = con.execute("""
        SELECT coalesce(m.category, '?') AS cat, avg(s.captured) AS rate
        FROM shadow_orders s
        LEFT JOIN markets m ON m.platform='betfair'
                           AND m.market_id = s.betfair_market_id
        WHERE s.captured IS NOT NULL
        GROUP BY cat
    """).fetchall()
    return {cat: rate for cat, rate in rows if rate is not None}


def main() -> None:
    db = PROJECT_ROOT / "data" / "lastshot.db"
    if not db.exists():
        sys.exit("no data/lastshot.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    has_table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_trades'"
    ).fetchone()
    if not has_table:
        sys.exit("no live_trades table yet — run the new tracker once (it creates it)")

    rows = con.execute("""
        SELECT category, pair_status, edge_intended, edge_realized, unwind_cost,
               decide_us, pm_to_bf_gap_ms, total_ms, pm_fill_source
        FROM live_trades
        WHERE pair_status IS NOT NULL AND pair_status != 'pending'
    """).fetchall()
    if not rows:
        sys.exit("no resolved live trades yet — arm the executor (CP1) first")

    print(f"=== Live arb outcomes ({len(rows)} trades) ===")
    # per-category counts by status
    groups: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for cat, status, *_ in rows:
        groups[cat][status] += 1
        groups["ALL"][status] += 1

    print(f"{'category':12s} {'n':>4s} {'lock%':>6s} {'abort%':>7s} {'unwind%':>8s} {'err':>4s}")
    for cat, c in sorted(groups.items()):
        n = sum(c.values())
        locked, neither = c.get("locked", 0), c.get("neither", 0)
        unwound, err = c.get("unwound", 0), c.get("error", 0)
        print(f"{cat:12s} {n:>4d} {100*locked/n:>5.0f}% {100*neither/n:>6.0f}% "
              f"{100*unwound/n:>7.0f}% {err:>4d}")

    # realized-vs-intended edge slippage on locked trades
    slips = [ei - er for _, st, ei, er, *_ in rows
             if st == "locked" and ei is not None and er is not None]
    if slips:
        slips.sort()
        q = lambda p: slips[min(int(p * len(slips)), len(slips) - 1)]  # noqa: E731
        print(f"\nedge slippage (intended-realized) on locked: "
              f"median {q(0.5):+.4f}  p90 {q(0.9):+.4f}")
    unwind_costs = [r[4] for r in rows if r[1] == "unwound" and r[4] is not None]
    if unwind_costs:
        print(f"unwind cost: n={len(unwind_costs)} "
              f"mean {sum(unwind_costs)/len(unwind_costs):+.4f}  worst {max(unwind_costs):+.4f}")

    # latency: the leg-risk window
    gaps = sorted(r[6] for r in rows if r[6] is not None)
    if gaps:
        q = lambda p: gaps[min(int(p * len(gaps)), len(gaps) - 1)]  # noqa: E731
        print(f"\nPM-fill -> Betfair-hedge gap (the leg-risk window): "
              f"median {q(0.5):.0f}ms  p90 {q(0.9):.0f}ms  max {gaps[-1]:.0f}ms")
    decide = sorted(r[5] for r in rows if r[5] is not None)
    if decide:
        q = lambda p: decide[min(int(p * len(decide)), len(decide) - 1)]  # noqa: E731
        print(f"decision latency (signal->order-ready): median {q(0.5):.0f}us  p90 {q(0.9):.0f}us")
    sources = defaultdict(int)
    for r in rows:
        if r[8]:
            sources[r[8]] += 1
    if sources:
        print("PM fill source:", dict(sources))

    # headline: live lock rate vs shadow capture rate, per category
    shadow = _shadow_capture_by_cat(con)
    print(f"\n=== live lock rate vs shadow capture rate ===")
    print(f"{'category':12s} {'live lock%':>10s} {'shadow cap%':>12s}")
    for cat, c in sorted(groups.items()):
        if cat == "ALL":
            continue
        n = sum(c.values())
        live_lock = 100 * c.get("locked", 0) / n
        sh = shadow.get(cat)
        sh_s = f"{100*sh:>10.1f}%" if sh is not None else f"{'n/a':>11s}"
        print(f"{cat:12s} {live_lock:>9.0f}% {sh_s}")

    print("\nNote: live n is small (one-shot / early soak) — a smoke test, not a "
          "statistic. Shadow PM rates are an upper bound (invisible cancels).")


if __name__ == "__main__":
    main()
