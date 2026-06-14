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
from dataclasses import dataclass

from ..storage import Store

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


def _num(v) -> float | None:
    """Best-effort float; None if missing/unparseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class FillResult:
    """Outcome of confirming a Polymarket FOK order. size is in shares."""
    size: float
    avg_price: float
    source: str        # post_response | get_order | shadow
    raw: dict


class PolymarketExecutor:
    def __init__(self, store: Store, private_key: str = "", funder: str = "",
                 armed: bool = False, signature_type: int = 2):
        self.store = store
        self.armed = armed and bool(private_key)
        # dry-run only: simulate a fractional fill in confirm_fill (None -> full
        # fill). Read only when not armed; production armed runs ignore it.
        self._sim_fill_fraction: float | None = None
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
        from py_clob_client_v2.exceptions import PolyApiException

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
            try:
                resp = await asyncio.to_thread(self._client.post_order, signed, ot)
            except PolyApiException as e:
                msg = e.error_msg if isinstance(e.error_msg, dict) else {"error": str(e.error_msg)}
                err = str(msg.get("error", "")).lower()
                # A killed FOK (no full fill) is reported as a 400, not a normal
                # response. It means ZERO fill / no exposure — a clean STATE A
                # abort, not an error. Anything else is a real failure -> re-raise.
                if "killed" in err or "couldn't be fully filled" in err:
                    ack |= {"dry_run": False, "sent": True, "status": "killed",
                            "order_id": msg.get("orderID"), "error": "FOK_KILLED",
                            "resp": msg}
                    ack["rtt_ms"] = (time.perf_counter_ns() - t0) / 1e6
                    self.store.save_exec_event("polymarket_order", ack)
                    return ack
                raise
            ack |= {"dry_run": False, "sent": True, "status": resp.get("status"),
                    "order_id": resp.get("orderID"), "error": resp.get("errorMsg"),
                    "resp": resp}

        ack["rtt_ms"] = (time.perf_counter_ns() - t0) / 1e6
        self.store.save_exec_event("polymarket_order", ack)
        return ack

    async def confirm_fill(self, place_ack: dict) -> FillResult:
        """Resolve how many shares a FOK order actually matched.

        Fast path parses the post_order response (size_matched, if present);
        otherwise falls back to get_order with a hard cap of 2 quick polls (FOK
        reaches a terminal state immediately, so this is just for eventual
        consistency). Biased to *verify* rather than assume zero — we never
        declare a no-fill on an inconclusive response without a get_order check,
        so we can't carry an undetected naked leg. The PM-fill->confirm latency
        IS the leg-risk window, so this stays bounded.
        """
        order = place_ack.get("order", {}) or {}
        intended_size = float(order.get("size", 0.0) or 0.0)
        px = _num(place_ack.get("snapped_price")) or _num(order.get("price")) or 0.0
        status = place_ack.get("status")

        # Dry-run / shadow / no creds: simulate a fill so the whole state machine
        # is exercisable without real money.
        if (self._client is None or place_ack.get("dry_run")
                or status in ("SHADOW", "BUILT_NOT_SENT")):
            frac = 1.0 if self._sim_fill_fraction is None else self._sim_fill_fraction
            return FillResult(size=round(intended_size * frac, 2), avg_price=px,
                              source="shadow", raw=place_ack)

        # Killed FOK: place() already classified it as a clean no-fill.
        if status == "killed" or place_ack.get("error") == "FOK_KILLED":
            return FillResult(size=0.0, avg_price=px, source="killed",
                              raw=place_ack.get("resp", {}))

        import asyncio

        # Fast path: matched size already in the post response.
        resp = place_ack.get("resp") or {}
        sm = _num(resp.get("size_matched")) or _num(resp.get("sizeMatched"))
        if sm is not None:
            return FillResult(size=sm, avg_price=_num(resp.get("price")) or px,
                              source="post_response", raw=resp)

        # Fallback: poll get_order (<=2 quick attempts).
        oid = place_ack.get("order_id")
        if not oid:
            return FillResult(size=0.0, avg_price=px, source="post_response", raw=resp)
        terminal = {"MATCHED", "FILLED", "CANCELED", "CANCELLED", "UNMATCHED", "KILLED"}
        last: dict = {}
        for attempt in range(2):
            try:
                last = await asyncio.to_thread(self._client.get_order, oid) or {}
            except Exception as e:  # noqa: BLE001 — confirmation must not crash the arb
                logger.warning("get_order failed (attempt %d): %s", attempt, e)
                last = {}
            sm = _num(last.get("size_matched")) or _num(last.get("sizeMatched"))
            st = str(last.get("status", "")).upper()
            if sm is not None and (sm > 0 or st in terminal):
                return FillResult(size=sm, avg_price=_num(last.get("price")) or px,
                                  source="get_order", raw=last)
            if attempt == 0:
                await asyncio.sleep(0.15)
        sm = _num(last.get("size_matched")) or _num(last.get("sizeMatched")) or 0.0
        return FillResult(size=sm, avg_price=_num(last.get("price")) or px,
                          source="get_order", raw=last)

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

    async def get_order(self, order_id: str) -> dict:
        """Raw CLOB order record (for polling resting-order fills). The maker
        side has no fill websocket, so it polls this. {} if no client/order."""
        if self._client is None or not order_id:
            return {}
        import asyncio
        try:
            return await asyncio.to_thread(self._client.get_order, order_id) or {}
        except Exception as e:  # noqa: BLE001 — polling must not crash the loop
            logger.warning("get_order(%s) failed: %s", str(order_id)[:14], e)
            return {}

    async def open_order_ids(self) -> list[str]:
        """All currently-open order ids on the account (for startup cleanup of
        orphans left by an ungraceful shutdown). [] if no client."""
        if self._client is None:
            return []
        import asyncio
        try:
            orders = await asyncio.to_thread(self._client.get_open_orders) or []
            return [o.get("id") for o in orders if o.get("id")]
        except Exception as e:  # noqa: BLE001
            logger.warning("open_order_ids failed: %s", e)
            return []

    async def cancel_all(self, order_ids: list[str]) -> int:
        """Best-effort cancel of the given resting orders. Returns count cancelled.
        Used as the maker safety rail (disarm / shutdown / stale)."""
        n = 0
        for oid in list(order_ids):
            try:
                ack = await self.cancel(oid)
                n += int(bool(ack.get("cancelled")))
            except Exception as e:  # noqa: BLE001 — never let one failure block the rest
                logger.error("cancel_all: failed to cancel %s: %s", str(oid)[:14], e)
        return n
