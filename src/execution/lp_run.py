"""Standalone runner for the Polymarket LP (liquidity-reward) market-making bot.

Selects mean-reverting reward markets (reusing lp_quoter.scan_candidates + allocate),
builds MarketStates, and runs the LpExecutor loops. Places NO orders unless BOTH
config `execution.armed: true` AND env `EXECUTOR_ARMED=1` (settings.py enforces this);
otherwise every order is BUILT_NOT_SENT / SHADOW.

    python -m src.execution.lp_run            # shadow/dry-run per config+env
    EXECUTOR_ARMED=1 python -m src.execution.lp_run   # live (if config armed + creds)

Capital ceiling and risk knobs come from config.yaml -> execution.lp.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone

from ..settings import load_settings
from ..storage import Store
from .polymarket_executor import PolymarketExecutor
from .lp_quoter import scan_candidates, allocate
from .lp_executor import LpExecutor, MarketState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("lp_run")


def _end_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _quotable(c: dict, blackout_hours: float, inv_book_frac: float) -> bool:
    """A candidate is worth funding only if it will actually quote: not within the
    blackout window of resolution, and with enough two-sided book that our min_size
    stays <= inv_book_frac of the resulting book (the same gate the executor applies)."""
    ets = _end_ts(c.get("end_date"))
    if ets is not None:
        hrs = (ets - datetime.now(timezone.utc).timestamp()) / 3600.0
        if hrs < blackout_hours:
            return False
    thin = c["min_size"] * (1 - inv_book_frac) / inv_book_frac
    return c.get("qual", 0) >= thin * 1.5   # 1.5x headroom so it quotes through book wiggle


def _to_market_state(c: dict) -> MarketState | None:
    if not (c.get("condition_id") and c.get("yes_token") and c.get("no_token")):
        return None
    return MarketState(
        condition_id=c["condition_id"], question=c["q"],
        yes_token=c["yes_token"], no_token=c["no_token"],
        tick=c["tick"], max_spread=c["max_spread"] / 100.0, min_size=c["min_size"],
        pool_day=c["pool"], qual=c["qual"], end_ts=_end_ts(c.get("end_date")),
        mid=c["mid"])


async def main() -> None:
    cfg = load_settings()
    ex = cfg.get("execution", {})
    lp = ex.get("lp", {})
    if not lp.get("enabled", False):
        logger.error("execution.lp.enabled is false — refusing to run. Set it true to start.")
        return
    store = Store(cfg["data_dir"])
    armed = bool(ex.get("armed", False))

    logger.info("LP selection: scanning reward markets (scan_limit=%s, min_pool=%s, "
                "max_min_size=%s)...", lp.get("scan_limit", 60), lp.get("min_pool", 20),
                lp.get("max_min_size", 100))
    cands = scan_candidates(scan_limit=lp.get("scan_limit", 60),
                            min_pool=lp.get("min_pool", 20.0),
                            max_min_size=lp.get("max_min_size", 100),
                            hurdle_mo_pct=lp.get("hurdle_mo_pct", 10.0))
    quotable = [c for c in cands
                if _quotable(c, lp.get("blackout_hours", 48.0), lp.get("inv_book_frac", 0.5))]
    logger.info("LP: %d scanned, %d quotable (not blacked-out + book deep enough)",
                len(cands), len(quotable))
    chosen, spent = allocate(quotable, lp.get("budget_usd", 500.0))
    markets = [m for m in (_to_market_state(c) for c in chosen) if m]
    if not markets:
        logger.error("no qualifying markets selected — nothing to quote. Exiting.")
        return
    logger.info("LP selected %d markets, ~$%.0f budget-fit. armed=%s",
                len(markets), spent, armed)
    for m in markets:
        logger.info("  %-42s pool $%.0f/d min %d mid %.3f", m.question[:42],
                    m.pool_day, int(m.min_size), m.mid)

    pm = PolymarketExecutor(store, private_key=cfg["poly_private_key"],
                            funder=cfg["poly_funder"], armed=armed,
                            signature_type=cfg["poly_signature_type"])

    lpx = LpExecutor(
        markets=markets, pm_exec=pm, store=store, armed=armed,
        refresh_s=lp.get("refresh_s", 20.0), poll_s=lp.get("poll_s", 5.0),
        book_poll_s=lp.get("book_poll_s", 10.0),
        reward_poll_s=lp.get("reward_poll_s", 900.0), metrics_s=lp.get("metrics_s", 60.0),
        reprice_eps=lp.get("reprice_eps", 0.005),
        half_spread_ticks=lp.get("half_spread_ticks", 1),
        max_open_quotes=lp.get("max_open_quotes", 20),
        budget_usd=lp.get("budget_usd", 500.0), size_mult=lp.get("size_mult", 1.0),
        inv_cap_mult=lp.get("inv_cap_mult", 3.0), inv_book_frac=lp.get("inv_book_frac", 0.5),
        jump_halt_pct=lp.get("jump_halt_pct", 0.15),
        kill_drawdown_frac=lp.get("kill_drawdown_frac", 0.15),
        blackout_hours=lp.get("blackout_hours", 48.0),
        min_pm_notional=ex.get("min_pm_notional", 1.0),
        maker_address=cfg["poly_funder"], signature_type=cfg["poly_signature_type"])

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lpx.shutdown)
        except NotImplementedError:
            pass

    try:
        await asyncio.gather(
            lpx.book_refresh_loop(), lpx.quoting_loop(), lpx.fill_poll_loop(),
            lpx.reward_poll_loop(), lpx.metrics_loop())
    finally:
        logger.warning("LP shutting down — cancelling all resting quotes")
        await lpx.cancel_all()


if __name__ == "__main__":
    asyncio.run(main())
