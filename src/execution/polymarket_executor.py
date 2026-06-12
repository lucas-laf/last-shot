"""Polymarket leg execution via the CLOB API, behind a dry-run flag.

Until credentials are provided (POLY_PRIVATE_KEY / POLY_FUNDER in .env) every
order is a dry run: the exact OrderArgs we would post are built and logged
with timings, so the hot path is exercised end-to-end minus the network call.
py-clob-client is imported lazily — the laptop install doesn't need it.
"""
from __future__ import annotations

import logging
import time

from ..storage import Store

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


class PolymarketExecutor:
    def __init__(self, store: Store, private_key: str = "", funder: str = "",
                 armed: bool = False, signature_type: int = 2):
        self.store = store
        self.armed = armed and bool(private_key)
        self._client = None
        if private_key:
            from py_clob_client.client import ClobClient  # lazy: heavy deps
            self._client = ClobClient(
                CLOB_HOST, key=private_key, chain_id=POLYGON_CHAIN_ID,
                funder=funder or None,
                signature_type=signature_type if funder else None,
            )
            self._client.set_api_creds(self._client.create_or_derive_api_creds())
            logger.info("Polymarket CLOB client ready (armed=%s)", self.armed)
        else:
            logger.info("Polymarket executor in DRY_RUN (no credentials)")

    def build_order(self, token_id: str, side: str, price: float, size: float) -> dict:
        """side: 'buy'/'sell' on the YES token; price = raw book price,
        size = shares. Kept as a plain dict so dry-run needs no SDK."""
        return {
            "token_id": token_id,
            "side": "BUY" if side == "buy" else "SELL",
            "price": round(price, 3),
            "size": round(size, 2),
        }

    async def place(self, order: dict) -> dict:
        t0 = time.perf_counter_ns()
        if not self.armed or self._client is None:
            ack = {"dry_run": True, "status": "SHADOW"}
        else:
            import asyncio

            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            args = OrderArgs(
                token_id=order["token_id"],
                side=BUY if order["side"] == "BUY" else SELL,
                price=order["price"],
                size=order["size"],
            )
            signed = await asyncio.to_thread(self._client.create_order, args)
            resp = await asyncio.to_thread(
                self._client.post_order, signed, OrderType.FOK)
            ack = {"dry_run": False, "status": resp.get("status"),
                   "order_id": resp.get("orderID"), "error": resp.get("errorMsg")}
        ack["rtt_ms"] = (time.perf_counter_ns() - t0) / 1e6
        ack["order"] = order
        self.store.save_exec_event("polymarket_order", ack)
        return ack
