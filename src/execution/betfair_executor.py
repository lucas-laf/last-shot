"""Betfair leg execution: FILL_OR_KILL limit orders at the crossing price.

Latency posture: the authenticated session is kept alive by a background
task, order payloads are templated per runner so the hot path only fills in
price/size, and odds are snapped to Betfair's tick ladder in the marketable
direction (never request a price the book can't match).

Disarmed (the default) it logs exactly what it would send, with timings.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

from betfairlightweight import APIClient, filters

from ..storage import Store

logger = logging.getLogger(__name__)

# Betfair decimal-odds tick ladder: (upper_bound, increment)
LADDER = [
    (2.0, 0.01), (3.0, 0.02), (4.0, 0.05), (6.0, 0.1), (10.0, 0.2),
    (20.0, 0.5), (30.0, 1.0), (50.0, 2.0), (100.0, 5.0), (1000.0, 10.0),
]
MIN_ODDS, MAX_ODDS = 1.01, 1000.0


def snap_odds(odds: float, side: str) -> float:
    """Snap decimal odds to the ladder, in the direction that still crosses
    the displayed quote: BACK rounds odds down, LAY rounds odds up."""
    odds = min(max(odds, MIN_ODDS), MAX_ODDS)
    lo = MIN_ODDS
    for hi, inc in LADDER:
        if odds <= hi + 1e-9:
            steps = (odds - lo) / inc
            n = math.floor(steps + 1e-9) if side == "BACK" else math.ceil(steps - 1e-9)
            return round(min(lo + n * inc, hi), 2)
        lo = hi
    return MAX_ODDS


def prob_to_marketable_odds(prob: float, side: str) -> float:
    """Convert an implied probability to ladder odds that cross the book.
    side is the Betfair side: BACK (our 'buy') or LAY (our 'sell')."""
    return snap_odds(1.0 / max(prob, 1e-6), side)


class BetfairExecutor:
    def __init__(self, client: APIClient, store: Store, armed: bool = False,
                 min_stake_gbp: float = 2.0):
        self.client = client
        self.store = store
        self.armed = armed
        self.min_stake = min_stake_gbp

    async def keep_alive_loop(self, interval_s: float = 3600.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await asyncio.to_thread(self.client.keep_alive)
                logger.info("Betfair keep-alive ok")
            except Exception as e:  # noqa: BLE001 — must never kill the tracker
                logger.error("Betfair keep-alive failed: %s", e)

    def build_order(self, market_id: str, selection_id: str, side: str,
                    prob: float, size_gbp: float) -> dict:
        """side: 'buy' (BACK) or 'sell' (LAY), prob = raw book price."""
        bf_side = "BACK" if side == "buy" else "LAY"
        return {
            "market_id": market_id,
            "instructions": [filters.place_instruction(
                order_type="LIMIT",
                selection_id=int(selection_id),
                side=bf_side,
                limit_order=filters.limit_order(
                    price=prob_to_marketable_odds(prob, bf_side),
                    size=round(max(size_gbp, self.min_stake), 2),
                    persistence_type="LAPSE",
                    time_in_force="FILL_OR_KILL",
                ),
            )],
        }

    async def place(self, order: dict) -> dict:
        """Send (or dry-run) a pre-built order. Returns an ack dict with
        status, sizeMatched and the venue round-trip in ms."""
        t0 = time.perf_counter_ns()
        if not self.armed:
            ack = {"dry_run": True, "status": "SHADOW", "order": order}
        else:
            report = await asyncio.to_thread(
                self.client.betting.place_orders,
                market_id=order["market_id"],
                instructions=order["instructions"],
            )
            inst = report.instruction_reports[0] if report.instruction_reports else None
            ack = {
                "dry_run": False,
                "status": report.status,
                "order_status": getattr(inst, "order_status", None),
                "size_matched": getattr(inst, "size_matched", 0.0),
                "price_matched": getattr(inst, "average_price_matched", None),
            }
        ack["rtt_ms"] = (time.perf_counter_ns() - t0) / 1e6
        self.store.save_exec_event("betfair_order", ack if not ack.get("dry_run")
                                   else {k: v for k, v in ack.items() if k != "order"})
        return ack
