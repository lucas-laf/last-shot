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
from ..execution.arb_executor import ArbExecutor
from ..execution.betfair_executor import BetfairExecutor
from ..execution.latency import RttMonitor
from ..execution.maker_executor import MakerExecutor
from ..execution.polymarket_executor import PolymarketExecutor
from ..models import MatchStatus, Platform, Tick
from ..settings import load_settings
from ..signals.engine import PairState, SignalEngine
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
        no_token = next((o.no_token_id for o in pm.outcomes
                         if o.outcome_id == link.polymarket_token_id), None)
        states.append(PairState(
            betfair_market_id=p.betfair_market_id,
            betfair_selection_id=link.betfair_selection_id,
            polymarket_market_id=p.polymarket_market_id,
            polymarket_token_id=link.polymarket_token_id,
            outcome_name=link.name,
            polymarket_no_token_id=no_token or "",
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
        convergence_enabled=cfg["signals"].get("convergence_enabled", True),
    )

    client = make_client(cfg)
    backoff = cfg["tracking"]["reconnect_backoff_seconds"]
    pm_feed: PolymarketFeed
    bf_feed: BetfairFeed

    async def on_tick(tick: Tick) -> None:
        writer.write(tick)
        arb_exec.on_tick(tick)
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
    # Paper trading dropped 2026-06-13: the executor is live (armed soak), so the
    # hypothetical paper trades are redundant noise. Shadow capture + live_trades
    # are the measurement now. (PaperTrader remains available for offline replay.)
    ex = cfg.get("execution", {})
    rtt = RttMonitor(store, session_token=client.session_token or "",
                     app_key=client.app_key)
    arb_exec = ArbExecutor(
        store=store,
        bf_exec=BetfairExecutor(client, store, armed=ex.get("armed", False)),
        pm_exec=PolymarketExecutor(store, private_key=cfg.get("poly_private_key", ""),
                                   funder=cfg.get("poly_funder", ""),
                                   armed=ex.get("armed", False),
                                   signature_type=cfg.get("poly_signature_type", 2)),
        rtt=rtt,
        categories={m.market_id: m.category
                    for m in store.get_markets(Platform.BETFAIR, active_only=False)},
        armed=ex.get("armed", False),
        live_categories=tuple(ex.get("live_categories", ["soccer", "politics"])),
        max_shares_per_leg=ex.get("max_shares_per_leg", 5.0),
        max_arbs_per_outcome=ex.get("max_arbs_per_outcome", 1),
        max_daily_capital=ex.get("max_daily_capital", 50.0),
        one_shot=ex.get("one_shot", True),
        max_live_arbs=ex.get("max_live_arbs", 1),
        min_pm_notional=ex.get("min_pm_notional", 3.0),
        min_bf_stake_gbp=ex.get("min_bf_stake_gbp", 2.0),
    )

    def on_signal(sig):
        arb_exec.on_signal(sig)

    engine.on_signal = on_signal

    # Maker-side executor (optional): rests passive PM quotes, hedges on fill.
    # Shares the same bf/pm executors (armed state + Betfair session) as the taker.
    mk = ex.get("maker", {})
    maker = None
    if mk.get("enabled"):
        maker = MakerExecutor(
            states=states, bf_exec=arb_exec.bf_exec, pm_exec=arb_exec.pm_exec,
            store=store, rtt=rtt, categories=arb_exec.categories,
            armed=ex.get("armed", False),
            commission=cfg["signals"]["betfair_commission"],
            margin=mk.get("margin", 0.01), refresh_s=mk.get("refresh_s", 0.5),
            poll_s=mk.get("poll_s", 0.5), cancel_stale_s=mk.get("cancel_stale_s", 3.0),
            reprice_eps=mk.get("reprice_eps", 0.01),
            max_open_quotes=mk.get("max_open_quotes", 8),
            quote_shares=mk.get("quote_shares", 5.0),
            min_quote_shares=mk.get("min_quote_shares", 2.0),
            one_shot=mk.get("one_shot", True), max_live_arbs=mk.get("max_live_arbs", 1),
            live_categories=tuple(mk.get("categories", ["tennis"])),
            float_usd=mk.get("float_usd", 80.0),
            min_pm_notional=ex.get("min_pm_notional", 1.0),
            min_bf_stake_gbp=ex.get("min_bf_stake_gbp", 2.0))
        logger.info("MakerExecutor enabled (categories=%s, margin=%.4f, one_shot=%s)",
                    maker.live_categories, maker.margin, maker.one_shot)

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

    coros = [bf_feed.run(), pm_feed.run(), status(),
             rtt.run(), arb_exec.bf_exec.keep_alive_loop()]
    if maker:
        coros += [maker.quoting_loop(), maker.fill_poll_loop()]
    try:
        await asyncio.gather(*coros)
    finally:
        writer.flush()
        if maker:
            await maker.cancel_all()   # never leave a resting order live


if __name__ == "__main__":
    asyncio.run(main())
