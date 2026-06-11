"""Replay recorded ticks through the live SignalEngine under alternative
convergence-reference policies: `python -m analysis.replay` (project root).

Answers "what would have happened if the convergence rule had treated
<platform>'s mid as the true probability?" using the same engine, fee model,
cooldowns and stake-capping as live tracking — only the reference policy
differs. Lock-arb signals are policy-independent and reported once.

Results come from already-settled paper trades in the DB (each platform's own
resolution), optionally topped up from the Gamma API. `--proxy-resolution`
settles a leg with the *other* platform's result when its own is unknown —
useful before Polymarket resolves, but it hides resolution-rule mismatches.

Usage:
  python -m analysis.replay                          # compare all 3 policies
  python -m analysis.replay --policy betfair
  python -m analysis.replay --fetch-pm-results --proxy-resolution
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.models import MatchStatus, Platform, Quote  # noqa: E402
from src.settings import load_settings  # noqa: E402
from src.signals import fees  # noqa: E402
from src.signals.engine import PairState, SignalEngine  # noqa: E402
from src.signals.settle import fetch_polymarket_results, pnl  # noqa: E402
from src.storage import Store  # noqa: E402

POLICIES = ("deeper", "betfair", "polymarket")
EDGE_BUCKETS = (0.005, 0.01, 0.02, 0.05)


@dataclass
class ReplayTrade:
    ts: datetime
    signal_type: str
    bet_platform: str
    side: str
    entry_prob: float
    edge: float
    stake: float
    betfair_market_id: str
    polymarket_market_id: str
    outcome_name: str
    stream_key: tuple  # (platform, market_id, outcome_id) of the bet platform
    taker_rate: float
    bf_spread: float = 1.0  # both books' spreads at entry, for quality filters
    pm_spread: float = 1.0
    won: bool | None = None
    pnl: float | None = None
    proxied: bool = False
    best_exit: float | None = None   # best later cash-out, pnl per share
    final_mark: float | None = None  # cash-out at the last recorded tick


def build_states(store: Store) -> list[PairState]:
    """Like tracking.run.build_states, but keeps inactive (resolved) markets."""
    pm_markets = {m.market_id: m for m in store.get_markets(Platform.POLYMARKET, active_only=False)}
    bf_markets = {m.market_id: m for m in store.get_markets(Platform.BETFAIR, active_only=False)}
    states = []
    for p in store.get_pairs([MatchStatus.AUTO_ACCEPTED, MatchStatus.APPROVED]):
        if not p.verdict.outcome_mapping:
            continue
        link = p.verdict.outcome_mapping[0]
        bf, pm = bf_markets.get(p.betfair_market_id), pm_markets.get(p.polymarket_market_id)
        if not bf or not pm:
            continue
        states.append(PairState(
            betfair_market_id=p.betfair_market_id,
            betfair_selection_id=link.betfair_selection_id,
            polymarket_market_id=p.polymarket_market_id,
            polymarket_token_id=link.polymarket_token_id,
            outcome_name=link.name,
            pm_taker_rate=pm.taker_fee,
            bf_liquidity=bf.liquidity,
            pm_liquidity=pm.liquidity,
        ))
    return states


def load_results(store: Store, states: list[PairState], fetch_pm: bool):
    """Outcome results per platform, derived from settled paper trades.

    bf_result[(bf_market, pm_market)] -> outcome happened (Betfair resolution)
    pm_result[pm_market]              -> YES won            (Polymarket resolution)
    """
    bf_result: dict[tuple[str, str], bool] = {}
    pm_result: dict[str, bool] = {}
    with store._conn as conn:
        rows = conn.execute(
            """SELECT betfair_market_id, polymarket_market_id, bet_platform, side, won
               FROM paper_trades WHERE settled=1"""
        ).fetchall()
    for bf_id, pm_id, platform, side, won in rows:
        # paper_trades.won stores whether the OUTCOME happened (runner won /
        # YES resolved), for sell legs too — settle.pnl handles the inversion.
        happened = bool(won)
        if platform == Platform.BETFAIR.value:
            bf_result[(bf_id, pm_id)] = happened
        else:
            pm_result[pm_id] = happened
    if fetch_pm:
        missing = sorted({s.polymarket_market_id for s in states
                          if s.polymarket_market_id not in pm_result})
        if missing:
            print(f"fetching Polymarket resolutions for {len(missing)} markets ...")
            pm_result.update(fetch_polymarket_results(missing))
    return bf_result, pm_result


def load_ticks(since: str | None, until: str | None) -> list[tuple]:
    glob = PROJECT_ROOT / "data" / "ticks" / "*.parquet"
    where = ["bid > 0", "ask < 1"]
    if since:
        where.append(f"ts >= '{since}'")
    if until:
        where.append(f"ts <= '{until}'")
    # epoch() instead of raw ts: duckdb->python tz conversion needs pytz,
    # which isn't installed in this venv.
    return duckdb.sql(f"""
        SELECT epoch(ts), platform, market_id, outcome_id, bid, ask, bid_size, ask_size
        FROM read_parquet('{glob}') WHERE {' AND '.join(where)} ORDER BY ts
    """).fetchall()


def run_policy(policy: str, states: list[PairState], ticks: list[tuple],
               cfg: dict) -> list[ReplayTrade]:
    states = [replace(s, last_fired={}) for s in states]
    by_bf = {(s.betfair_market_id, s.betfair_selection_id): s for s in states}
    by_pm = {s.polymarket_token_id: s for s in states}
    engine = SignalEngine(
        commission=cfg["signals"]["betfair_commission"],
        min_edge=cfg["signals"]["min_edge"],
        convergence_ref=policy,
    )
    max_stake = cfg["signals"]["max_paper_stake"]
    trades: list[ReplayTrade] = []

    for epoch_s, platform, market_id, outcome_id, bid, ask, bid_size, ask_size in ticks:
        ts = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
        quote = Quote(bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)
        if platform == Platform.BETFAIR.value:
            state = by_bf.get((market_id, outcome_id))
            if state:
                state.bf, state.bf_ts = quote, ts
        else:
            state = by_pm.get(outcome_id)
            if state:
                state.pm, state.pm_ts = quote, ts
        if not state:
            continue
        for sig in engine.evaluate(state, now=ts):
            q = state.bf if sig.bet_platform == Platform.BETFAIR else state.pm
            displayed = q.ask_size if sig.side == "buy" else q.bid_size
            stake = min(max_stake, displayed)
            if stake <= 0:
                continue
            if sig.bet_platform == Platform.BETFAIR:
                key = (Platform.BETFAIR.value, state.betfair_market_id,
                       state.betfair_selection_id)
            else:
                key = (Platform.POLYMARKET.value, state.polymarket_market_id,
                       state.polymarket_token_id)
            trades.append(ReplayTrade(
                ts=ts, signal_type=sig.signal_type.value,
                bet_platform=sig.bet_platform.value, side=sig.side,
                entry_prob=sig.entry_prob, edge=sig.edge_after_fees, stake=stake,
                betfair_market_id=state.betfair_market_id,
                polymarket_market_id=state.polymarket_market_id,
                outcome_name=state.outcome_name,
                stream_key=key, taker_rate=state.pm_taker_rate,
                bf_spread=state.bf.spread, pm_spread=state.pm.spread,
            ))
    return trades


def build_streams(ticks: list[tuple]) -> dict[tuple, tuple]:
    """(platform, market_id, outcome_id) -> (ts_arr, bid_arr, ask_arr)."""
    raw: dict[tuple, list[tuple]] = defaultdict(list)
    for epoch_s, platform, market_id, outcome_id, bid, ask, *_ in ticks:
        raw[(platform, market_id, outcome_id)].append((epoch_s, bid, ask))
    return {k: ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])
            for k, rows in raw.items()}


def _exit_fns(t: ReplayTrade, commission: float):
    if t.bet_platform == Platform.BETFAIR.value:
        return (lambda p: fees.betfair_sell(p, commission),
                lambda p: fees.betfair_buy(p, commission))
    return (lambda p: fees.polymarket_sell(p, t.taker_rate),
            lambda p: fees.polymarket_buy(p, t.taker_rate))


def _unwind_profit(t: ReplayTrade, bid: float, ask: float, commission: float) -> float:
    sell, buy = _exit_fns(t, commission)
    return sell(bid) - t.entry_prob if t.side == "buy" else t.entry_prob - buy(ask)


def compute_exits(trades: list[ReplayTrade], streams: dict, commission: float) -> None:
    """Best and final fee-adjusted cash-out for each trade, from later ticks
    on the same platform/outcome. Best exit is hindsight-optimal: it bounds
    what any exit rule could have achieved; final_mark is mark-to-market at
    the end of the recorded data."""
    from bisect import bisect_right

    # suffix max bid / min ask per stream (exit prices are monotone in these)
    suffix: dict[tuple, tuple] = {}
    for key, (ts_arr, bids, asks) in streams.items():
        n = len(ts_arr)
        max_bid, min_ask = [0.0] * n, [0.0] * n
        mb, ma = 0.0, 1.0
        for i in range(n - 1, -1, -1):
            mb, ma = max(mb, bids[i]), min(ma, asks[i])
            max_bid[i], min_ask[i] = mb, ma
        suffix[key] = (ts_arr, max_bid, min_ask, (bids[-1], asks[-1]))

    for t in trades:
        if t.stream_key not in suffix:
            continue
        ts_arr, max_bid, min_ask, (last_bid, last_ask) = suffix[t.stream_key]
        i = bisect_right(ts_arr, t.ts.timestamp())
        if i >= len(ts_arr):
            continue
        sell, buy = _exit_fns(t, commission)
        if t.side == "buy":
            t.best_exit = sell(max_bid[i]) - t.entry_prob
            t.final_mark = sell(last_bid) - t.entry_prob
        else:
            t.best_exit = t.entry_prob - buy(min_ask[i])
            t.final_mark = t.entry_prob - buy(last_ask)


def simulate_take_profit(label: str, trades: list[ReplayTrade], streams: dict,
                         commission: float, thresholds: tuple = (0.01, 0.02, 0.05)) -> None:
    """Exit at the first tick where unwinding clears the threshold per share
    (fees both ways); otherwise hold to settlement. Trades that neither exit
    nor settle are excluded from the realized number ('open')."""
    from bisect import bisect_right

    # rows: threshold -> [n_exited, n_held_settled, n_open, pnl, stake]
    rows = {thr: [0, 0, 0, 0.0, 0.0] for thr in thresholds}
    for t in trades:
        stream = streams.get(t.stream_key)
        hit: dict[float, float] = {}
        if stream:
            ts_arr, bids, asks = stream
            pending = sorted(thresholds)
            for j in range(bisect_right(ts_arr, t.ts.timestamp()), len(ts_arr)):
                profit = _unwind_profit(t, bids[j], asks[j], commission)
                while pending and profit >= pending[0]:
                    hit[pending.pop(0)] = profit
                if not pending:
                    break
        for thr, row in rows.items():
            if thr in hit:
                row[0] += 1
                row[3] += hit[thr] * t.stake
                row[4] += t.stake
            elif t.pnl is not None:
                row[1] += 1
                row[3] += t.pnl
                row[4] += t.stake
            else:
                row[2] += 1

    settled = [t for t in trades if t.pnl is not None]
    print(f"\n--- {label}: take-profit rules ---")
    if settled:
        base_stake = sum(t.stake for t in settled)
        print(f"  hold to settlement: n={len(settled)}, "
              f"{sum(t.pnl for t in settled) / base_stake:+.4f}/share")
    for thr, (n_exit, n_hold, n_open, total, stake) in sorted(rows.items()):
        per = f"{total / stake:+.4f}" if stake else "-"
        print(f"  exit at +{thr:.2f}: exited {n_exit}, held-to-settle {n_hold},"
              f" open {n_open}  ->  pnl {total:+.2f} ({per}/share)")


def settle(trades: list[ReplayTrade], bf_result: dict, pm_result: dict,
           proxy: bool) -> None:
    for t in trades:
        pair = (t.betfair_market_id, t.polymarket_market_id)
        if t.bet_platform == Platform.BETFAIR.value:
            won = bf_result.get(pair)
            if won is None and proxy and t.polymarket_market_id in pm_result:
                won, t.proxied = pm_result[t.polymarket_market_id], True
        else:
            won = pm_result.get(t.polymarket_market_id)
            if won is None and proxy and pair in bf_result:
                won, t.proxied = bf_result[pair], True
        if won is not None:
            t.won = won  # outcome happened; settle.pnl inverts for sells
            t.pnl = pnl(t.side, t.entry_prob, t.stake, won)


def summarize(label: str, trades: list[ReplayTrade]) -> None:
    settled = [t for t in trades if t.pnl is not None]
    print(f"\n--- {label}: {len(trades)} trades, {len(settled)} settled"
          f" ({sum(t.proxied for t in settled)} via proxy resolution) ---")
    if not settled:
        return
    total = sum(t.pnl for t in settled)
    stake = sum(t.stake for t in settled)
    print(f"  realized pnl {total:+.2f} on {stake:.0f} staked"
          f"  ({total / stake:+.4f}/share)")
    rows = defaultdict(lambda: [0, 0.0, 0.0])
    for t in settled:
        r = rows[(t.bet_platform, t.side)]
        r[0] += 1
        r[1] += t.pnl
        r[2] += t.stake
    for (platform, side), (n, p, s) in sorted(rows.items()):
        print(f"  {platform:10s} {side:4s}  n={n:<5d} pnl {p:+9.2f}  ({p / s:+.4f}/share)")


def summarize_events(label: str, trades: list[ReplayTrade]) -> None:
    """Event-level (per Betfair market) returns for settled trades. Trades on
    the same event are correlated (cooldown re-fires, both runners of a match
    settle together), so the honest sample size and error bars live here."""
    events: dict[str, list[ReplayTrade]] = defaultdict(list)
    for t in trades:
        if t.pnl is not None:
            events[t.betfair_market_id].append(t)
    if not events:
        return
    returns = [sum(t.pnl for t in g) / sum(t.stake for t in g)
               for g in events.values()]
    n = len(returns)
    mean = sum(returns) / n
    if n > 1:
        var = sum((r - mean) ** 2 for r in returns) / (n - 1)
        se = (var / n) ** 0.5
        stats = f"mean {mean:+.4f}/share  se {se:.4f}  t {mean / se:+.2f}" \
            if se > 0 else f"mean {mean:+.4f}/share"
    else:
        stats = f"mean {mean:+.4f}/share"
    wins = sum(r > 0 for r in returns)
    print(f"\n--- {label}: event-level (settled) ---")
    print(f"  {n} events, {wins} profitable: {stats}")


def summarize_edges(label: str, trades: list[ReplayTrade]) -> None:
    """Hold-to-settlement return and cash-out potential by entry-edge size."""
    def bucket(e: float) -> str:
        lo = 0.0
        for hi in EDGE_BUCKETS:
            if e < hi:
                return f"{lo:.3f}-{hi:.3f}"
            lo = hi
        return f">{EDGE_BUCKETS[-1]:.3f}"

    groups: dict[str, list[ReplayTrade]] = defaultdict(list)
    for t in trades:
        groups[bucket(t.edge)].append(t)
    print(f"\n--- {label}: by entry edge after fees ---")
    print(f"  {'edge':12s} {'n':>5s} {'settled':>8s} {'hold/share':>11s}"
          f" {'exit>0':>7s} {'best_exit':>10s} {'final_mark':>11s}")
    for name in sorted(groups):
        g = groups[name]
        settled = [t for t in g if t.pnl is not None]
        hold = (f"{sum(t.pnl for t in settled) / sum(t.stake for t in settled):+.4f}"
                if settled else "      -")
        ex = [t for t in g if t.best_exit is not None]
        if ex:
            exitable = f"{100 * sum(t.best_exit > 0 for t in ex) / len(ex):.0f}%"
            best = f"{sum(t.best_exit for t in ex) / len(ex):+.4f}"
            mark = f"{sum(t.final_mark for t in ex) / len(ex):+.4f}"
        else:
            exitable = best = mark = "-"
        print(f"  {name:12s} {len(g):>5d} {len(settled):>8d} {hold:>11s}"
              f" {exitable:>7s} {best:>10s} {mark:>11s}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--policy", choices=POLICIES, help="replay one policy (default: all)")
    ap.add_argument("--since", help="ISO timestamp lower bound on ticks")
    ap.add_argument("--until", help="ISO timestamp upper bound on ticks")
    ap.add_argument("--fetch-pm-results", action="store_true",
                    help="query Gamma for resolutions missing from the DB")
    ap.add_argument("--proxy-resolution", action="store_true",
                    help="settle a leg with the other platform's result if its own is unknown")
    args = ap.parse_args()

    cfg = load_settings()
    store = Store(cfg["data_dir"])
    states = build_states(store)
    if not states:
        sys.exit("no approved pairs in the DB")
    ticks = load_ticks(args.since, args.until)
    print(f"{len(states)} pairs, {len(ticks)} ticks"
          f" (min_edge={cfg['signals']['min_edge']},"
          f" commission={cfg['signals']['betfair_commission']})")
    bf_result, pm_result = load_results(store, states, args.fetch_pm_results)
    print(f"results known: {len(bf_result)} betfair outcomes, {len(pm_result)} polymarket markets")

    streams = build_streams(ticks)
    commission = cfg["signals"]["betfair_commission"]
    policies = [args.policy] if args.policy else list(POLICIES)
    for i, policy in enumerate(policies):
        trades = run_policy(policy, states, ticks, cfg)
        settle(trades, bf_result, pm_result, args.proxy_resolution)
        compute_exits(trades, streams, commission)
        conv = [t for t in trades if t.signal_type == "convergence"]
        summarize(f"convergence, ref={policy}", conv)
        summarize_events(f"convergence, ref={policy}", conv)
        summarize_edges(f"convergence, ref={policy}", conv)
        simulate_take_profit(f"convergence, ref={policy}", conv, streams, commission)
        if i == 0:  # identical under every policy
            arb = [t for t in trades if t.signal_type == "lock_arb"]
            summarize("lock_arb (policy-independent)", arb)
            summarize_events("lock_arb (policy-independent)", arb)
            # cash-out of a single lock_arb leg breaks the hedge; edge table
            # is still useful for the hold-to-settlement return.
            summarize_edges("lock_arb (policy-independent)", arb)


if __name__ == "__main__":
    main()
