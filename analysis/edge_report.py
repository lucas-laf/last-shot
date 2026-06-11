"""Edge report: `python -m analysis.edge_report` (run from project root).

The go/no-go input for live execution: realized paper PnL after fees by
category and signal type, signal frequency, and platform lead/lag.
Caveats baked in: fills are hypothetical (size-capped at displayed depth),
and GBP/USDC FX is ignored.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    db = PROJECT_ROOT / "data" / "lastshot.db"
    ticks = PROJECT_ROOT / "data" / "ticks" / "*.parquet"
    if not db.exists():
        sys.exit("no data/lastshot.db — run the pipeline first")

    con = duckdb.connect()
    con.execute(f"ATTACH '{db}' AS app (TYPE sqlite)")

    print("=== Paper trades by signal type ===")
    # ROI: convergence is single-sided (capital = entry for buys, 1-entry for
    # sells). A lock-arb pair ties up ~$1/share across both legs, so its ROI
    # on capital is ~the edge itself — never quote its per-leg ROI.
    con.sql("""
        SELECT signal_type,
               count(*)                      AS trades,
               sum(settled)                  AS settled,
               round(avg(edge_after_fees),4) AS avg_edge,
               round(100*median(CASE WHEN signal_type='lock_arb'
                     THEN edge_after_fees / (1 - edge_after_fees)
                     ELSE edge_after_fees / (CASE WHEN side='buy' THEN entry_prob ELSE 1-entry_prob END)
                   END),2)                   AS median_roi_pct,
               round(sum(pnl),2)             AS realized_pnl,
               round(sum(stake),0)           AS total_stake
        FROM app.paper_trades GROUP BY 1 ORDER BY 1
    """).show(max_width=200)

    print("\n=== Paper trades by platform bet on ===")
    con.sql("""
        SELECT bet_platform, side, count(*) AS trades,
               round(avg(edge_after_fees),4) AS avg_edge,
               round(sum(pnl),2) AS realized_pnl
        FROM app.paper_trades GROUP BY 1,2 ORDER BY 1,2
    """).show(max_width=200)

    print("\n=== Realized PnL by category (settled trades) ===")
    con.sql("""
        SELECT m.category,
               count(*) AS trades,
               round(sum(t.pnl),2) AS realized_pnl,
               round(avg(t.pnl/nullif(t.stake,0)),4) AS pnl_per_share
        FROM app.paper_trades t
        JOIN app.markets m
          ON m.platform='betfair' AND m.market_id=t.betfair_market_id
        WHERE t.settled=1
        GROUP BY 1 ORDER BY realized_pnl DESC
    """).show(max_width=200)

    tick_files = list((PROJECT_ROOT / "data" / "ticks").glob("*.parquet"))
    if tick_files:
        print("\n=== Tick volume by platform/hour ===")
        con.sql(f"""
            SELECT date_trunc('hour', ts) AS hour, platform, count(*) AS ticks,
                   count(DISTINCT market_id) AS markets
            FROM read_parquet('{ticks}')
            GROUP BY 1,2 ORDER BY 1 DESC, 2 LIMIT 20
        """).show(max_width=200)

        print("\n=== Largest mid divergences seen (top of book, last day) ===")
        con.sql(f"""
            WITH t AS (
                SELECT *, (bid+ask)/2 AS mid FROM read_parquet('{ticks}')
                WHERE ts > now() - INTERVAL 1 DAY AND bid > 0 AND ask < 1
            ),
            bf AS (SELECT market_id, outcome_id, avg(mid) AS bf_mid,
                          avg(ask-bid) AS bf_spread FROM t
                   WHERE platform='betfair' GROUP BY 1,2),
            pm AS (SELECT market_id, avg(mid) AS pm_mid,
                          avg(ask-bid) AS pm_spread FROM t
                   WHERE platform='polymarket' GROUP BY 1)
            SELECT json_extract_string(v.verdict_json, '$.outcome_mapping[0].name') AS outcome,
                   v.betfair_market_id, v.polymarket_market_id,
                   round(bf.bf_mid,3) AS bf_mid, round(pm.pm_mid,3) AS pm_mid,
                   round(abs(bf.bf_mid-pm.pm_mid),3) AS diff
            FROM app.match_verdicts v
            JOIN bf ON bf.market_id = v.betfair_market_id
                   AND bf.outcome_id = json_extract_string(
                         v.verdict_json, '$.outcome_mapping[0].betfair_selection_id')
            JOIN pm ON pm.market_id = v.polymarket_market_id
            WHERE json_extract_string(v.verdict_json, '$.is_match') = 'true'
              -- wide books make the mid meaningless, not divergent
              AND bf.bf_spread < 0.15 AND pm.pm_spread < 0.15
            ORDER BY diff DESC LIMIT 15
        """).show(max_width=200)
    else:
        print("\n(no tick files yet — run the tracker)")


if __name__ == "__main__":
    main()
