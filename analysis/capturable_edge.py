"""'Unlimited capital' ceiling: total capturable lock-arb edge in the recorded
books. For each matched pair we take the single best pre-match dislocation
(edge x min(PM depth, BF depth), in USD) — one sweep per market — then sum and
scale to monthly. Computed in DuckDB via an ASOF join (aligns each PM tick with
the latest Betfair quote) so it stays fast.

UPPER BOUND: assumes displayed depth is real and fully takeable at the quote
(no phantom liquidity, no market impact, full fill). Real capture is lower.

Usage: python -m analysis.capturable_edge [--min-edge 0.01] [--include-inplay]
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from src.models import MatchStatus, Platform  # noqa: E402
from src.settings import load_settings  # noqa: E402
from src.storage import Store  # noqa: E402

TICK_DIR = Path("data/ticks")
GBP_USD = 1.27


def tick_files():
    fs = sorted(p for p in TICK_DIR.glob("ticks_*.parquet") if p.stat().st_size > 1000)
    return [str(p) for p in (fs[:-1] if len(fs) > 1 else fs)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-edge", type=float, default=None)
    ap.add_argument("--commission", type=float, default=None)
    ap.add_argument("--include-inplay", action="store_true")
    args = ap.parse_args()

    cfg = load_settings()
    store = Store(cfg["data_dir"])
    c = args.commission if args.commission is not None else cfg["signals"]["betfair_commission"]
    me = args.min_edge if args.min_edge is not None else cfg["signals"]["min_edge"]

    bf_m = {m.market_id: m for m in store.get_markets(Platform.BETFAIR, active_only=False)}
    pm_fee = {m.market_id: m.taker_fee for m in store.get_markets(Platform.POLYMARKET, active_only=False)}
    pairs = []
    for p in store.get_pairs([MatchStatus.AUTO_ACCEPTED, MatchStatus.APPROVED]):
        bf = bf_m.get(p.betfair_market_id)
        if not bf or not p.verdict.outcome_mapping:
            continue
        link = p.verdict.outcome_mapping[0]
        st = int(bf.start_time.timestamp() * 1000) if bf.start_time else None
        pairs.append((p.betfair_market_id, link.betfair_selection_id,
                      link.polymarket_token_id, float(pm_fee.get(p.polymarket_market_id, 0.0) or 0.0),
                      bf.category or "?", st))

    con = duckdb.connect()
    files = tick_files()
    con.execute("create table pairs(bfm varchar, bfs varchar, tok varchar, r double, cat varchar, st bigint)")
    con.executemany("insert into pairs values (?,?,?,?,?,?)", pairs)
    con.execute("""create table bf as select market_id m, outcome_id s, epoch_ms(ts) t,
                   bid, ask, bid_size bs, ask_size az from read_parquet(?) where platform='betfair'""", [files])
    con.execute("""create table pm as select outcome_id tok, epoch_ms(ts) t,
                   bid, ask, bid_size bs, ask_size az from read_parquet(?) where platform='polymarket'""", [files])
    span = con.execute("select (max(t)-min(t))/86400000.0 from (select t from bf union all select t from pm)").fetchone()[0]
    inplay_ok = "true" if args.include_inplay else "(pm.t < coalesce(p.st, 9000000000000))"

    # betfair_sell(p)=p*(1-c); betfair_buy(p)=1/(1+(1/p-1)*(1-c))
    # pm_buy(p)=p+r*least(p,1-p); pm_sell(p)=p-r*least(p,1-p)
    q = f"""
    with rows as (
      select p.cat, p.bfm, p.bfs, p.tok, p.r,
             pm.bid pmb, pm.ask pma, pm.bs pmbs, pm.az pmas,
             bf.bid bfb, bf.ask bfa, bf.bs bfbs, bf.az bfas
      from pairs p
      join pm on pm.tok = p.tok and {inplay_ok}
      asof join bf on p.bfm = bf.m and p.bfs = bf.s and bf.t <= pm.t
      where bf.bid > 0 and pm.bid > 0 and pm.ask < 1 and bf.ask > 0
    ),
    cap as (
      select cat, bfm, bfs, tok, max(greatest(
        case when (bfb*(1-{c}) - (pma + r*least(pma,1-pma))) >= {me}
             then (bfb*(1-{c}) - (pma + r*least(pma,1-pma))) * least(pmas*pma, bfbs*{GBP_USD}) else 0 end,
        case when ((pmb - r*least(pmb,1-pmb)) - 1.0/(1+(1.0/bfa-1)*(1-{c}))) >= {me}
             then ((pmb - r*least(pmb,1-pmb)) - 1.0/(1+(1.0/bfa-1)*(1-{c}))) * least(pmbs*pmb, bfas*{GBP_USD}) else 0 end
      )) best from rows group by cat, bfm, bfs, tok
    )
    select cat, count(*) filter (where best>0) npairs, sum(best) total from cap group by cat order by total desc
    """
    res = con.execute(q).fetchall()
    print(f"recorded span: {span:.2f} days | min_edge={me} commission={c} | "
          f"{'INCL in-play' if args.include_inplay else 'PRE-MATCH only'} | model: best single sweep per market")
    print(f"{'category':12s}{'mkts w/ arb':>12s}{'capturable $':>14s}{'$/month':>11s}")
    tot = 0.0; ntot = 0
    for cat, n, total in res:
        total = total or 0.0; tot += total; ntot += (n or 0)
        print(f"{cat:12s}{n or 0:>12d}{total:>14.2f}{total/span*30:>11.0f}")
    print(f"{'TOTAL':12s}{ntot:>12d}{tot:>14.2f}{tot/span*30:>11.0f}")
    print("\nUPPER BOUND — displayed depth assumed real & fully takeable. Real capture lower\n"
          "(phantom liquidity + market impact). One sweep/market; refreshing liquidity not counted.")


if __name__ == "__main__":
    main()
