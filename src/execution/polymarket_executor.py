"""Polymarket leg execution via the CLOB **V2** API, behind a dry-run flag.

Polymarket migrated to CLOB V2 on 2026-04-28: the EIP-712 exchange domain
version moved 1->2 and a new V2 Exchange contract was deployed, so the old
``py-clob-client`` (V1) signs orders the server now rejects with
``invalid order version``. This module uses ``py-clob-client-v2``. (The earlier
``invalid order version`` failure was misdiagnosed as a neg-risk routing bug;
it was the V2 migration. See the project memory.)

Order building stays tick- and neg-risk-aware: prices must sit on the market's
tick grid (0.01 or 0.001, per market) and multi-outcome markets route through
the neg-risk exchange. We resolve ``tick_size`` and ``neg_risk`` per token
(SDK-cached) and pass them explicitly into ``create_order``, snapping the price
to the resolved tick. Under V2 the signed struct no longer carries
fee_rate_bps/nonce/taker — fees are protocol-determined at match time.

Three execution states:
- no credentials               -> pure SHADOW (no client; nothing is built)
- credentials, armed=False     -> BUILT_NOT_SENT (order is built + signed and
                                  the resolved params logged, but never posted)
- credentials, armed=True      -> posted to the CLOB

Signing is local; only ``post_order`` moves money. py-clob-client-v2 is
imported lazily — the laptop install doesn't need it.
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
            from py_clob_client_v2.client import ClobClient  # lazy: heavy deps
            self._client = ClobClient(
                CLOB_HOST, POLYGON_CHAIN_ID, key=private_key,
                funder=funder or None,
                signature_type=signature_type if funder else None,
            )
            self._client.set_api_creds(self._client.create_or_derive_api_key())
            logger.info("Polymarket CLOB V2 client ready (armed=%s)", self.armed)
        else:
            logger.info("Polymarket executor in DRY_RUN (no credentials)")

    def build_order(self, token_id: str, side: str, price: float, size: float,
                    neg_risk: bool | None = None, tick_size: str | None = None) -> dict:
        """side: 'buy'/'sell' on the YES token; price = raw book price,
        size = shares. neg_risk/tick_size are optional hints — if omitted they
        are resolved from the CLOB at place() time. Kept as a plain dict so the
        shadow path needs no SDK."""
        order = {
            "token_id": token_id,
            "side": "BUY" if side == "buy" else "SELL",
            "price": float(price),
            "size": round(size, 2),
        }
        if neg_risk is not None:
            order["neg_risk"] = neg_risk
        if tick_size is not None:
            order["tick_size"] = tick_size
        return order

    @staticmethod
    def _snap_to_tick(price: float, tick: str) -> float:
        """Snap a raw price onto the market's tick grid and clamp into
        (tick, 1-tick) — off-grid or boundary prices are rejected by the CLOB."""
        t = float(tick)
        decimals = len(tick.split(".")[1]) if "." in tick else 0
        snapped = round(round(price / t) * t, decimals)
        return min(max(snapped, t), round(1 - t, decimals))

    async def place(self, order: dict, order_type: str = "FOK") -> dict:
        """Build, sign, and (only if armed) post one Polymarket order.

        order_type: 'FOK' (taker, fill-or-kill — the arb hot path) or 'GTC'
        (resting limit — used by the test harness for zero-fill acceptance
        checks). Resolves tick_size + neg_risk per token (SDK-cached) and snaps
        the price before signing.
        """
        t0 = time.perf_counter_ns()
        token_id = order["token_id"]
        ack: dict = {"order": order}

        if self._client is None:
            ack |= {"dry_run": True, "sent": False, "status": "SHADOW"}
            ack["rtt_ms"] = (time.perf_counter_ns() - t0) / 1e6
            self.store.save_exec_event("polymarket_order", ack)
            return ack

        import asyncio

        from py_clob_client_v2 import Side
        from py_clob_client_v2.clob_types import (OrderArgs, OrderType,
                                                  PartialCreateOrderOptions)

        # Resolve market params (cached per token after the first lookup).
        tick = order.get("tick_size") or await asyncio.to_thread(
            self._client.get_tick_size, token_id)
        neg_risk = order.get("neg_risk")
        if neg_risk is None:
            neg_risk = await asyncio.to_thread(self._client.get_neg_risk, token_id)
        price = self._snap_to_tick(order["price"], tick)

        # V2 OrderArgs drops fee_rate_bps/nonce/taker; fees are set by the
        # protocol at match time.
        args = OrderArgs(
            token_id=token_id,
            side=Side.BUY if order["side"] == "BUY" else Side.SELL,
            price=price,
            size=order["size"],
        )
        opts = PartialCreateOrderOptions(tick_size=tick, neg_risk=bool(neg_risk))
        signed = await asyncio.to_thread(self._client.create_order, args, opts)
        ack |= {"neg_risk": bool(neg_risk), "tick_size": tick,
                "snapped_price": price, "built": True}

        if not self.armed:
            ack |= {"dry_run": True, "sent": False, "status": "BUILT_NOT_SENT"}
        else:
            ot = OrderType.GTC if order_type == "GTC" else OrderType.FOK
            resp = await asyncio.to_thread(self._client.post_order, signed, ot)
            ack |= {"dry_run": False, "sent": True, "status": resp.get("status"),
                    "order_id": resp.get("orderID"), "error": resp.get("errorMsg")}

        ack["rtt_ms"] = (time.perf_counter_ns() - t0) / 1e6
        self.store.save_exec_event("polymarket_order", ack)
        return ack

    async def cancel(self, order_id: str) -> dict:
        """Cancel a resting order by id (used to clean up test orders)."""
        if self._client is None:
            return {"cancelled": False, "reason": "no_client"}
        import asyncio

        from py_clob_client_v2.clob_types import OrderPayload
        resp = await asyncio.to_thread(
            self._client.cancel_order, OrderPayload(orderID=order_id))
        ack = {"cancelled": True, "order_id": order_id, "resp": resp}
        self.store.save_exec_event("polymarket_cancel", ack)
        return ack
