"""Track all matched pairs in real time: `python -m src.tracking.run`.

Wires both feeds into shared PairState, evaluates the signal engine on every
update, records ticks to parquet and signals to the paper-trade ledger.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from ..betfair_client import make_client
from ..models import MatchStatus, Platform, Tick
from ..settings import load_settings
from ..signals.engine import PairState, SignalEngine
from ..signals.paper_trader import PaperTrader
from ..storage import Store, TickWriter
from .betfair_stream import BetfairFeed
from .polymarket_ws import PolymarketFeed

logger = logging.getLogger(__name__)


def build_states(store: Store) -> list[PairState]:
    pairs = store.get_pairs([MatchStatus.AUTO_ACCEPTED, MatchStatus.APPROVED])
    bf_markets = {m.market_id: m for m in store.get_markets(Platform.BETFAIR, active_only=False)}
    pm_markets = {m.market_id: m for m in store.get_markets(Platform.POLYMARKET, active_only=False)}
    states = []
    for p in pairs:
        if not p.verdict.outcome_mapping:
            continue
        link = p.verdict.outcome_mapping[0]
        bf = bf_markets.get(p.betfair_market_id)
        pm = pm_markets.get(p.polymarket_market_id)
        if not bf or not pm or not bf.active or not pm.active:
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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-every", type=float, default=30.0,
                        help="seconds between status summaries")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_settings()
    store = Store(cfg["data_dir"])
    states = build_states(store)
    if not states:
        logger.error("No tracked pairs. Run discovery + matching (+ review) first.")
        return
    logger.info("Tracking %d matched outcomes", len(states))

    by_bf = {(s.betfair_market_id, s.betfair_selection_id): s for s in states}
    by_pm_token = {s.polymarket_token_id: s for s in states}

    writer = TickWriter(cfg["data_dir"], cfg["tracking"]["tick_flush_rows"])
    engine = SignalEngine(
        commission=cfg["signals"]["betfair_commission"],
        min_edge=cfg["signals"]["min_edge"],
        convergence_ref=cfg["signals"].get("convergence_ref", "deeper"),
        max_ref_spread=cfg["signals"].get("max_ref_spread"),
    )

    client = make_client(cfg)
    backoff = cfg["tracking"]["reconnect_backoff_seconds"]
    pm_feed: PolymarketFeed
    bf_feed: BetfairFeed

    async def on_tick(tick: Tick) -> None:
        writer.write(tick)
        if tick.platform == Platform.BETFAIR:
            state = by_bf.get((tick.market_id, tick.outcome_id))
            if state:
                state.bf, state.bf_ts = tick.quote, tick.ts
        else:
            state = by_pm_token.get(tick.outcome_id)
            if state:
                state.pm, state.pm_ts = tick.quote, tick.ts
        if state:
            engine.evaluate(state)

    bf_feed = BetfairFeed(client, set(by_bf), on_tick, backoff)
    pm_feed = PolymarketFeed(
        {s.polymarket_token_id: s.polymarket_market_id for s in states},
        on_tick, backoff,
    )
    trader = PaperTrader(
        store=store,
        max_stake=cfg["signals"]["max_paper_stake"],
        betfair_depth=bf_feed.depth,
        polymarket_depth=pm_feed.depth,
    )
    engine.on_signal = trader.on_signal

    async def status() -> None:
        while True:
            await asyncio.sleep(args.status_every)
            live = [s for s in states if s.bf and s.pm]
            lines = []
            for s in sorted(live, key=lambda x: -(x.bf.mid if x.bf else 0))[:10]:
                lines.append(
                    f"  {s.outcome_name[:30]:30s} bf {s.bf.bid:.3f}/{s.bf.ask:.3f}"
                    f"  pm {s.pm.bid:.3f}/{s.pm.ask:.3f}"
                    f"  diff {abs(s.bf.mid - s.pm.mid):.3f}"
                )
            logger.info("%d/%d outcomes have both quotes\n%s",
                        len(live), len(states), "\n".join(lines))
            writer.flush()

    try:
        await asyncio.gather(bf_feed.run(), pm_feed.run(), status())
    finally:
        writer.flush()


if __name__ == "__main__":
    asyncio.run(main())
