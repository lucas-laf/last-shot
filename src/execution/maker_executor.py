"""Maker-side lock-arb executor: rest a passive quote inside the Polymarket
spread, priced so an instant Betfair hedge locks >= margin; hedge on fill.

The higher-capacity, latency-tolerant alternative to the taker arb (which only
captured ~18% of fast tennis windows). We're filled by definition (a counterparty
hits our resting order) — no capture race. Pricing reuses the backtest math in
analysis/maker_sniper_backtest.py.

THE risk: py-clob-client-v2 has no fill websocket, so resting fills are POLLED;
the fill->hedge gap is ~poll_s/2 + bf_rtt (not the taker's ~24ms). The `margin`
buffer must exceed the expected Betfair move over that window, and the unwind
catches the tail. Safety rails: armed + maker.enabled, one_shot (one filled arb
then auto-disarm), min stakes, capital reservation, and cancel_all on every exit
path (never leave a resting order live unattended).

Two coroutines (added to the tracker's asyncio.gather, one event loop):
  quoting_loop   — every refresh_s, place/cancel/replace resting quotes.
  fill_poll_loop — every poll_s, poll resting orders; on fill -> hedge -> unwind.
Both mutate shared state under a single asyncio.Lock.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..signals import fees
from ..signals.engine import PairState
from ..storage import Store
from .betfair_executor import BetfairExecutor
from .latency import RttMonitor
from .polymarket_executor import PolymarketExecutor

logger = logging.getLogger(__name__)

STALE = "stale"


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class _Target:
    side: str            # "bid" | "ask" (logical PM-YES side)
    yes_price: float     # logical YES-space price (q_buy / q_sell)
    exec_token: str      # YES token for bid; NO token for ask (short)
    exec_price: float    # price we rest at on exec_token (snapped to 0.01 grid)
    pm_is_short: bool
    bf_ref: float        # Betfair price the quote was computed from (stale check)
    bf_hedge_side: str   # "sell" (LAY) for bid; "buy" (BACK) for ask
    locked: float        # margin captured per share if filled


@dataclass
class MakerQuote:
    pair_key: tuple
    state: PairState
    side: str
    exec_token: str
    exec_price: float
    pm_is_short: bool
    bf_ref: float
    bf_hedge_side: str
    locked: float
    size: float
    order_id: str
    placed_ts: float
    reserved_usd: float
    status: str = "resting"   # resting | filling
    reprice_count: int = 0


class MakerExecutor:
    def __init__(self, *, states: list[PairState], bf_exec: BetfairExecutor,
                 pm_exec: PolymarketExecutor, store: Store, rtt: RttMonitor,
                 categories: dict[str, str], armed: bool = False,
                 commission: float = 0.05, margin: float = 0.01,
                 refresh_s: float = 0.5, poll_s: float = 0.5,
                 cancel_stale_s: float = 3.0, reprice_eps: float = 0.01,
                 max_open_quotes: int = 8, quote_shares: float = 5.0,
                 min_quote_shares: float = 2.0, one_shot: bool = True,
                 max_live_arbs: int = 1, live_categories: tuple = ("tennis",),
                 float_usd: float = 80.0, min_pm_notional: float = 1.0,
                 min_bf_stake_gbp: float = 2.0):
        self.states = states
        self.bf_exec = bf_exec
        self.pm_exec = pm_exec
        self.store = store
        self.rtt = rtt
        self.categories = categories
        self.armed = armed
        self.commission = commission
        self.margin = margin
        self.refresh_s = refresh_s
        self.poll_s = poll_s
        self.cancel_stale_s = cancel_stale_s
        self.reprice_eps = reprice_eps
        self.max_open_quotes = max_open_quotes
        self.quote_shares = quote_shares
        self.min_quote_shares = min_quote_shares
        self.one_shot = one_shot
        self.max_live_arbs = max_live_arbs
        self.live_categories = tuple(live_categories)
        self.float_usd = float_usd
        self.min_pm_notional = min_pm_notional
        self.min_bf_stake_gbp = min_bf_stake_gbp

        self._quotes: dict[str, MakerQuote] = {}
        self._reserved_usd = 0.0
        self._live_count = 0
        self._inflight: set[str] = set()
        self._shutdown = False
        self._lock = asyncio.Lock()

    # ---------- pricing (pure, unit-testable) ----------

    def _compute_target(self, s: PairState, side: str) -> _Target | None:
        """The backtest math: rest inside the PM spread at a price that locks
        >= margin against an instant Betfair hedge. None if not quotable."""
        c = self.commission
        if side == "bid":   # rest BUY YES, hedge = LAY Betfair at bf.bid
            ref = fees.betfair_sell(s.bf.bid, c)
            q = math.floor((ref - self.margin) * 100) / 100   # 0.01 grid (valid on 0.001 too)
            if not (s.pm.bid < q < s.pm.ask):
                return None
            locked = ref - q
            tgt = _Target("bid", q, s.polymarket_token_id, q, False, s.bf.bid, "sell", locked)
        else:               # rest SELL YES = BUY NO @ (1-q), hedge = BACK Betfair at bf.ask
            if not s.polymarket_no_token_id:
                return None
            ref = fees.betfair_buy(s.bf.ask, c)
            q = math.ceil((ref + self.margin) * 100) / 100
            if not (s.pm.bid < q < s.pm.ask):
                return None
            locked = q - ref
            tgt = _Target("ask", q, s.polymarket_no_token_id, round(1.0 - q, 2),
                          True, s.bf.ask, "buy", locked)
        if (tgt.locked <= 0
                or self.quote_shares * tgt.exec_price < self.min_pm_notional
                or self.quote_shares * tgt.bf_ref < self.min_bf_stake_gbp):
            return None
        return tgt

    # ---------- eligibility ----------

    def _fresh(self, ts) -> bool:
        return bool(ts) and (datetime.now(timezone.utc) - ts).total_seconds() <= self.cancel_stale_s

    def _eligible(self, s: PairState) -> bool:
        return (self.categories.get(s.betfair_market_id) in self.live_categories
                and s.bf is not None and s.pm is not None
                and self._fresh(s.bf_ts) and self._fresh(s.pm_ts))

    def _should_quote(self) -> bool:
        return self.armed and not self._shutdown and self._live_count < self.max_live_arbs

    # ---------- quoting loop ----------

    async def quoting_loop(self) -> None:
        while not self._shutdown:
            await asyncio.sleep(self.refresh_s)
            if not self._should_quote():
                if self._quotes:
                    await self.cancel_all()
                continue
            async with self._lock:
                for s in self.states:
                    for side in ("bid", "ask"):
                        await self._reconcile(s, side)

    async def _reconcile(self, s: PairState, side: str) -> None:
        """Place / cancel / replace one resting quote. Lock held by caller."""
        key = f"{side}:{s.betfair_market_id}:{s.betfair_selection_id}"
        existing = self._quotes.get(key)
        target = self._compute_target(s, side) if self._eligible(s) else None

        if target is None:
            if existing and existing.status == "resting":
                await self._cancel_quote(key, reason=STALE)
            return

        if existing is None:
            # capacity + capital gates
            reserve = self.quote_shares * target.exec_price
            if (len(self._quotes) >= self.max_open_quotes
                    or self._reserved_usd + reserve > self.float_usd):
                return
            await self._place_quote(key, s, target, reserve)
            return

        if existing.status != "resting":
            return  # filling — leave it for the fill handler

        # reprice only on a material/adverse Betfair move (hysteresis vs rate limits)
        adverse = ((side == "bid" and target.exec_price < existing.exec_price)
                   or (side == "ask" and target.exec_price > existing.exec_price))
        moved = abs(target.bf_ref - existing.bf_ref) >= self.reprice_eps
        too_young = (time.perf_counter() - existing.placed_ts) < self.refresh_s
        if not adverse and (not moved or too_young):
            return
        rc = existing.reprice_count + 1
        await self._cancel_quote(key, reason="reprice")
        reserve = self.quote_shares * target.exec_price
        if self._reserved_usd + reserve <= self.float_usd:
            await self._place_quote(key, s, target, reserve, reprice_count=rc)

    async def _place_quote(self, key, s, t: _Target, reserve, reprice_count=0) -> None:
        order = self.pm_exec.build_order(t.exec_token, "buy", t.exec_price, self.quote_shares)
        ack = await self.pm_exec.place(order, order_type="GTC")
        oid = ack.get("order_id") or f"shadow-{key}"
        self._quotes[key] = MakerQuote(
            pair_key=(s.betfair_market_id, s.betfair_selection_id), state=s, side=t.side,
            exec_token=t.exec_token, exec_price=t.exec_price, pm_is_short=t.pm_is_short,
            bf_ref=t.bf_ref, bf_hedge_side=t.bf_hedge_side, locked=t.locked,
            size=self.quote_shares, order_id=oid, placed_ts=time.perf_counter(),
            reserved_usd=reserve, reprice_count=reprice_count)
        self._reserved_usd += reserve
        self.store.save_exec_event("maker_quote", {
            "key": key, "token": t.exec_token, "side": t.side, "price": t.exec_price,
            "size": self.quote_shares, "locked": round(t.locked, 4), "order_id": oid})

    async def _cancel_quote(self, key, reason="") -> None:
        q = self._quotes.pop(key, None)
        if not q:
            return
        self._reserved_usd -= q.reserved_usd
        if not str(q.order_id).startswith("shadow-"):
            await self.pm_exec.cancel(q.order_id)
        logger.info("maker cancel %s (%s)", key, reason)

    # ---------- fill poll loop ----------

    async def fill_poll_loop(self) -> None:
        while not self._shutdown:
            await asyncio.sleep(self.poll_s)
            if not self.armed:
                continue
            async with self._lock:
                snapshot = [(k, q) for k, q in self._quotes.items()
                            if q.status == "resting" and q.order_id not in self._inflight
                            and not str(q.order_id).startswith("shadow-")]
            for key, q in snapshot:
                info = await self.pm_exec.get_order(q.order_id)
                matched = _num(info.get("size_matched")) or _num(info.get("sizeMatched")) or 0.0
                status = str(info.get("status", "")).upper()
                if matched > 1e-6 or status in ("MATCHED", "FILLED"):
                    await self._handle_fill(key, q, matched, info)

    async def _handle_fill(self, key, q: MakerQuote, matched: float, info: dict) -> None:
        async with self._lock:
            if self._quotes.get(key) is not q or q.order_id in self._inflight:
                return  # raced with a cancel/replace — drop it
            self._inflight.add(q.order_id)
            q.status = "filling"
        fill_size = min(matched, q.size) if matched > 0 else q.size
        fill_detected = time.perf_counter()
        s = q.state
        bf_price = s.bf.bid if q.bf_hedge_side == "sell" else s.bf.ask
        base = {
            "decision_ts": datetime.now(timezone.utc).isoformat(),
            "betfair_market_id": s.betfair_market_id,
            "polymarket_market_id": s.polymarket_market_id,
            "outcome_name": s.outcome_name,
            "category": self.categories.get(s.betfair_market_id, "?"),
            "pm_side": "buy", "pm_intended_price": q.exec_price,
            "pm_intended_size": q.size, "pm_filled_price": q.exec_price,
            "pm_filled_size": fill_size, "pm_fill_source": "maker_resting",
            "pm_token_id": q.exec_token, "pm_is_short": int(q.pm_is_short),
            "bf_selection_id": s.betfair_selection_id, "bf_side": q.bf_hedge_side,
            "bf_intended_price": bf_price, "edge_intended": q.locked,
            "is_maker": 1, "resting_order_id": q.order_id,
            "time_resting_ms": (fill_detected - q.placed_ts) * 1e3,
            "reprice_count": q.reprice_count, "poll_s": self.poll_s,
        }
        trade_id = None
        try:
            self._live_count += 1
            base["pair_status"] = "pending"
            trade_id = self.store.save_live_trade(base)
            hedge_stake = round(fill_size * bf_price, 2)
            if hedge_stake < self.min_bf_stake_gbp - 1e-9:
                uw = await self._unwind(q, fill_size)
                self._finalize_unwind(trade_id, fill_detected, uw, bf=None)
            else:
                bf_order = self.bf_exec.build_order(
                    s.betfair_market_id, s.betfair_selection_id, q.bf_hedge_side,
                    bf_price, hedge_stake)
                ack_bf = await self.bf_exec.place(bf_order)
                bf = self.bf_exec.parse_fill(ack_bf)
                gap_ms = (time.perf_counter() - fill_detected) * 1e3
                if bf.matched_enough(hedge_stake):
                    self.store.update_live_trade(
                        trade_id, pair_status="locked", bf_filled_stake=bf.matched_stake,
                        bf_filled_price=(1.0 / bf.avg_odds) if bf.avg_odds else bf_price,
                        pm_to_bf_gap_ms=gap_ms, bf_rtt_ms=ack_bf.get("rtt_ms"),
                        edge_realized=q.locked)
                    logger.info("MAKER LOCKED %s shares=%.2f locked=%.4f gap=%.0fms",
                                s.outcome_name, fill_size, q.locked, gap_ms)
                else:
                    uw = await self._unwind(q, fill_size)
                    self._finalize_unwind(trade_id, fill_detected, uw, bf=bf,
                                          bf_rtt=ack_bf.get("rtt_ms"))
        except Exception as e:  # noqa: BLE001 — never leave silent naked exposure
            logger.exception("maker fill->hedge errored: %s", s.outcome_name)
            self.store.save_exec_event("unwind_alert",
                                       {"outcome": s.outcome_name, "error": str(e)})
            if trade_id is not None:
                self.store.update_live_trade(trade_id, pair_status="error")
        finally:
            async with self._lock:
                self._reserved_usd -= q.reserved_usd
                self._quotes.pop(key, None)
                self._inflight.discard(q.order_id)
            if self.one_shot and self._live_count >= self.max_live_arbs:
                await self.cancel_all()
                self._disarm()

    async def _unwind(self, q: MakerQuote, fill_size: float) -> dict:
        """Flatten a naked PM fill: marketable reverse FOK; GTC fallback + alert."""
        rev_price = 0.01   # we always BOUGHT exec_token, so reverse = SELL low to cross
        order = self.pm_exec.build_order(q.exec_token, "sell", rev_price, fill_size)
        ack = await self.pm_exec.place(order, order_type="FOK")
        close = await self.pm_exec.confirm_fill(ack)
        if close.size >= fill_size - 1e-6:
            cost = round((q.exec_price - close.avg_price) * fill_size, 4)
            self.store.save_exec_event("unwind", {"token": q.exec_token,
                                                  "flattened": True, "cost": cost})
            return {"flattened": True, "cost": cost}
        rest = self.pm_exec.build_order(q.exec_token, "sell", rev_price, fill_size)
        rest_ack = await self.pm_exec.place(rest, order_type="GTC")
        self.store.save_exec_event("unwind_alert", {
            "token": q.exec_token, "flattened": False,
            "naked_size": fill_size - close.size, "resting": rest_ack.get("order_id")})
        logger.error("MAKER UNWIND INCOMPLETE — naked %.2f; resting reverse placed",
                     fill_size - close.size)
        return {"flattened": False, "cost": None}

    def _finalize_unwind(self, trade_id, fill_detected, uw, bf, bf_rtt=None) -> None:
        self.store.update_live_trade(
            trade_id, pair_status="unwound", unwind_cost=uw.get("cost"),
            bf_filled_stake=(bf.matched_stake if bf else 0.0), bf_rtt_ms=bf_rtt,
            pm_to_bf_gap_ms=(time.perf_counter() - fill_detected) * 1e3)
        logger.warning("MAKER STATE C — unwound (flattened=%s, cost=%s)",
                       uw.get("flattened"), uw.get("cost"))

    # ---------- safety ----------

    async def cancel_all(self) -> None:
        async with self._lock:
            keys = list(self._quotes)
        for key in keys:
            await self._cancel_quote(key, reason="cancel_all")

    def _disarm(self) -> None:
        self.armed = False
        self.bf_exec.armed = False
        self.pm_exec.armed = False
        logger.warning("MAKER ONE-SHOT COMPLETE — auto-disarmed after %d arb(s)",
                       self._live_count)
        self.store.save_exec_event("maker_auto_disarm", {"live_count": self._live_count})
