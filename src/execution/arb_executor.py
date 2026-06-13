"""Lock-arb execution coordinator with shadow capture measurement.

Consumes lock_arb signals from the live SignalEngine (same on_signal hook as
the paper trader). For each arb it builds BOTH legs, sized to
min(bf displayed, pm displayed, cap) so the position is fully hedged — the
leg-size mismatch in the paper trader was the dominant variance source.

Shadow mode (the default, and always on for non-whitelisted categories):
instead of sending orders, each leg becomes a pending shadow order. Ticks
are then watched to decide whether the quote would still have been there
when our order arrived (decision time + measured venue RTT):

- a tick BEFORE arrival that makes the target price unexecutable -> killed_early
- the first tick AFTER arrival -> captured iff the last view was executable
- no tick for 120s -> the book never changed -> captured (push feeds only
  emit changes; caveat: invisible cancels on PM aren't observable)

Safety rails for armed mode: category whitelist, per-outcome arb limit,
daily deployed-capital cap, and ARMED defaults to false everywhere.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models import Platform, SignalType, Tick
from ..signals.engine import Signal
from ..storage import Store
from .betfair_executor import BetfairExecutor
from .latency import Hops, RttMonitor
from .polymarket_executor import PolymarketExecutor

logger = logging.getLogger(__name__)

SHADOW_TIMEOUT_S = 120.0


@dataclass
class PendingShadow:
    order_id: int
    stream_key: tuple          # (platform, market_id, outcome_id)
    side: str                  # buy | sell
    price: float               # raw book price we would cross
    decision_ts: float         # epoch seconds
    arrival_ts: float          # decision + venue rtt
    executable: bool = True    # last view of the book vs our target


@dataclass(frozen=True)
class ArbPlan:
    """Immutable snapshot of one arb to execute. Never hold a live PairState —
    it mutates on every tick between scheduling and execution."""
    pm_token_id: str
    pm_market_id: str
    pm_side: str               # buy | sell on the PM YES token
    pm_price: float            # raw book price we'd cross
    bf_market_id: str
    bf_selection_id: str
    bf_side: str               # buy (BACK) | sell (LAY)
    bf_price: float
    shares: float
    category: str
    outcome_name: str
    edge: float
    decide_us: float | None
    decision_ts: str


@dataclass
class ArbExecutor:
    store: Store
    bf_exec: BetfairExecutor
    pm_exec: PolymarketExecutor
    rtt: RttMonitor
    categories: dict[str, str]            # betfair_market_id -> category
    armed: bool = False
    live_categories: tuple = ("soccer", "politics")
    max_shares_per_leg: float = 5.0
    max_arbs_per_outcome: int = 1
    max_daily_capital: float = 50.0
    one_shot: bool = True                  # fire one real arb then auto-disarm
    max_live_arbs: int = 1                  # hard cap on real arbs
    min_pm_notional: float = 1.0            # PM enforces a $1 min order notional
    min_bf_stake_gbp: float = 2.0
    _pending: dict[tuple, list] = field(default_factory=dict)
    _arbs_by_outcome: dict[tuple, int] = field(default_factory=dict)
    _capital_day: str = ""
    _capital_used: float = 0.0
    _live_count: int = 0                    # real arbs actually sent (PM posted)
    _live_inflight: bool = False            # one arb executing at a time
    _live_lock: object = None

    def __post_init__(self) -> None:
        self._live_lock = asyncio.Lock()

    # ---------- signal entry ----------

    def on_signal(self, sig: Signal) -> None:
        """Engine emits two signals per lock_arb (one per leg); act once,
        on the buy leg, and derive the sell leg from the same pair state."""
        if sig.signal_type != SignalType.LOCK_ARB or sig.side != "buy":
            return
        s = sig.state
        h = Hops()
        if not s.bf or not s.pm:
            return

        if sig.bet_platform == Platform.BETFAIR:
            buy_key = (Platform.BETFAIR.value, s.betfair_market_id, s.betfair_selection_id)
            sell_key = (Platform.POLYMARKET.value, s.polymarket_market_id, s.polymarket_token_id)
            buy_price, buy_size = s.bf.ask, s.bf.ask_size
            sell_price, sell_size = s.pm.bid, s.pm.bid_size
            bf_side, bf_price = "buy", s.bf.ask
            pm_side, pm_price = "sell", s.pm.bid
        else:
            buy_key = (Platform.POLYMARKET.value, s.polymarket_market_id, s.polymarket_token_id)
            sell_key = (Platform.BETFAIR.value, s.betfair_market_id, s.betfair_selection_id)
            buy_price, buy_size = s.pm.ask, s.pm.ask_size
            sell_price, sell_size = s.bf.bid, s.bf.bid_size
            pm_side, pm_price = "buy", s.pm.ask
            bf_side, bf_price = "sell", s.bf.bid

        shares = min(buy_size, sell_size, self.max_shares_per_leg)
        if shares <= 0:
            return
        h.mark("sized")

        outcome = (s.betfair_market_id, s.betfair_selection_id)
        if self._arbs_by_outcome.get(outcome, 0) >= self.max_arbs_per_outcome:
            return
        self._arbs_by_outcome[outcome] = self._arbs_by_outcome.get(outcome, 0) + 1

        # Min-notional: skip the *live* path (still record shadows) if either
        # leg can't meet its venue minimum — build_order's max(size,min) clamp
        # would otherwise silently over-spend.
        pm_notional = shares * pm_price
        bf_stake = shares * bf_price
        notional_ok = (pm_notional >= self.min_pm_notional
                       and bf_stake >= self.min_bf_stake_gbp)

        # Only the PM-BUY leg is executable from a flat balance: selling a YES
        # token requires already holding it (a short = buying the NO token, not
        # yet implemented). PM-sell arbs stay shadow-only. [future: NO-token leg]
        category = self.categories.get(s.betfair_market_id, "?")
        go_live = (self.armed and category in self.live_categories
                   and pm_side == "buy" and notional_ok
                   and self._capital_ok(shares))
        h.mark("decision")

        for key, side, price in ((buy_key, "buy", buy_price),
                                 (sell_key, "sell", sell_price)):
            rtt = (self.rtt.betfair_rtt_ms() if key[0] == "betfair"
                   else self.rtt.polymarket_rtt_ms())
            self._record_shadow(s, key, side, price, shares, rtt, h)
        h.mark("shadow_recorded")

        # One-shot gate: check-then-claim the single slot synchronously (no await
        # before _live_inflight=True), so the cap is race-free on the loop thread.
        if go_live and self._should_fire_live():
            self._live_inflight = True
            plan = ArbPlan(
                pm_token_id=s.polymarket_token_id, pm_market_id=s.polymarket_market_id,
                pm_side=pm_side, pm_price=pm_price,
                bf_market_id=s.betfair_market_id, bf_selection_id=s.betfair_selection_id,
                bf_side=bf_side, bf_price=bf_price, shares=shares, category=category,
                outcome_name=s.outcome_name, edge=sig.edge_after_fees,
                decide_us=h.us("decision"),
                decision_ts=datetime.now(timezone.utc).isoformat(),
            )
            self._launch(self._execute_live(plan))

        self.store.save_exec_event("arb_decision", {
            "outcome": s.outcome_name, "category": category, "shares": shares,
            "edge": sig.edge_after_fees, "armed": go_live,
            "min_notional_ok": notional_ok, "hops_us": h.as_dict(),
        })

    # ---------- live execution (Phase B) ----------

    def _should_fire_live(self) -> bool:
        return (self.armed and not self._live_inflight
                and self._live_count < self.max_live_arbs)

    def _launch(self, coro) -> None:
        """Schedule the async arb on the running loop. on_signal runs on the
        loop thread (via the tracker's on_tick coroutine), so a loop exists in
        production; in tests without a loop we skip and release the slot."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("no running loop; cannot fire live arb")
            self._live_inflight = False
            coro.close()
            return
        loop.create_task(coro)

    def _disarm(self) -> None:
        self.armed = False
        self.bf_exec.armed = False
        self.pm_exec.armed = False
        logger.warning("ONE-SHOT COMPLETE — executor auto-disarmed after %d live arb(s)",
                       self._live_count)
        self.store.save_exec_event("auto_disarm", {"live_count": self._live_count})

    async def _execute_live(self, plan: ArbPlan) -> None:
        """PM leg first (perishable side) → hedge on Betfair sized to the ACTUAL
        PM fill → flatten PM if the hedge is killed (naked leg). Always writes
        exactly one live_trades row; never leaves silent naked exposure."""
        async with self._live_lock:
            t0 = time.perf_counter()
            trade_id = None
            base = {
                "decision_ts": plan.decision_ts,
                "betfair_market_id": plan.bf_market_id,
                "polymarket_market_id": plan.pm_market_id,
                "outcome_name": plan.outcome_name, "category": plan.category,
                "pm_side": plan.pm_side, "pm_intended_price": plan.pm_price,
                "pm_intended_size": plan.shares, "bf_side": plan.bf_side,
                "bf_intended_price": plan.bf_price,
                "bf_intended_stake": round(plan.shares * plan.bf_price, 2),
                "edge_intended": plan.edge, "decide_us": plan.decide_us,
            }
            try:
                pm_order = self.pm_exec.build_order(
                    plan.pm_token_id, plan.pm_side, plan.pm_price, plan.shares)
                ack_pm = await self.pm_exec.place(pm_order, order_type="FOK")
                t_pm = time.perf_counter()
                fill = await self.pm_exec.confirm_fill(ack_pm)
                base |= {"pm_filled_price": fill.avg_price, "pm_filled_size": fill.size,
                         "pm_fill_source": fill.source, "pm_rtt_ms": ack_pm.get("rtt_ms")}

                if fill.size <= 0:                                   # STATE A
                    base |= {"pair_status": "neither",
                             "total_ms": (time.perf_counter() - t0) * 1e3}
                    self.store.save_live_trade(base)
                    logger.info("ARB STATE A — PM killed, no fill: %s", plan.outcome_name)
                    return

                # PM filled → money has moved. Consume the shot + disarm now.
                self._live_count += 1
                base["pair_status"] = "pending"
                trade_id = self.store.save_live_trade(base)
                if self.one_shot and self._live_count >= self.max_live_arbs:
                    self._disarm()

                hedge_stake = round(fill.size * plan.bf_price, 2)
                # leg-risk window: PM fill confirmed -> Betfair order about to send
                gap_ms = (time.perf_counter() - t_pm) * 1e3
                if hedge_stake < self.min_bf_stake_gbp - 1e-9:
                    logger.warning("hedge stake £%.2f < min; unwinding PM", hedge_stake)
                    uw = await self._unwind_pm(plan, fill)
                    self._record_unwound(trade_id, gap_ms, t0, uw, bf=None, bf_rtt=None)
                    return

                bf_order = self.bf_exec.build_order(
                    plan.bf_market_id, plan.bf_selection_id, plan.bf_side,
                    plan.bf_price, hedge_stake)
                ack_bf = await self.bf_exec.place(bf_order)
                bf = self.bf_exec.parse_fill(ack_bf)

                if bf.matched_enough(hedge_stake):                   # STATE B: locked
                    self.store.update_live_trade(
                        trade_id, pair_status="locked",
                        bf_filled_stake=bf.matched_stake,
                        bf_filled_price=(1.0 / bf.avg_odds) if bf.avg_odds else plan.bf_price,
                        pm_to_bf_gap_ms=gap_ms, bf_rtt_ms=ack_bf.get("rtt_ms"),
                        edge_realized=plan.edge,
                        total_ms=(time.perf_counter() - t0) * 1e3)
                    logger.info("ARB STATE B — LOCKED: %s shares=%.2f gap=%.0fms",
                                plan.outcome_name, fill.size, gap_ms)
                else:                                                # STATE C: unwind
                    uw = await self._unwind_pm(plan, fill)
                    self._record_unwound(trade_id, gap_ms, t0, uw, bf=bf,
                                         bf_rtt=ack_bf.get("rtt_ms"))
            except Exception as e:  # noqa: BLE001 — never leave silent exposure
                logger.exception("live arb errored: %s", plan.outcome_name)
                if trade_id is not None:
                    # PM had already filled -> potential naked exposure -> alert
                    self.store.save_exec_event(
                        "unwind_alert", {"outcome": plan.outcome_name, "error": str(e)})
                    self.store.update_live_trade(trade_id, pair_status="error")
                else:
                    # rejected before any fill (e.g. balance/min) -> no exposure
                    self.store.save_live_trade({**base, "pair_status": "error"})
            finally:
                self._live_inflight = False

    async def _unwind_pm(self, plan: ArbPlan, fill) -> dict:
        """Immediate flatten: marketable reverse FOK sized to the PM fill. On
        failure, rest a reverse limit and raise an alert (never silent naked)."""
        reverse = "sell" if plan.pm_side == "buy" else "buy"
        # Cross aggressively: a limit at the far extreme fills against the touch
        # (Polymarket fills marketable limits at the resting maker price).
        rev_price = 0.99 if reverse == "buy" else 0.01
        order = self.pm_exec.build_order(plan.pm_token_id, reverse, rev_price, fill.size)
        ack = await self.pm_exec.place(order, order_type="FOK")
        close = await self.pm_exec.confirm_fill(ack)
        if close.size >= fill.size - 1e-6:
            cost = self._unwind_cost(plan.pm_side, fill.avg_price, close.avg_price, fill.size)
            self.store.save_exec_event("unwind", {
                "outcome": plan.outcome_name, "flattened": True,
                "size": close.size, "cost": cost})
            return {"flattened": True, "cost": cost, "close": close}
        # fallback: resting reverse + alert
        rest = self.pm_exec.build_order(plan.pm_token_id, reverse, rev_price, fill.size)
        rest_ack = await self.pm_exec.place(rest, order_type="GTC")
        naked = fill.size - close.size
        self.store.save_exec_event("unwind_alert", {
            "outcome": plan.outcome_name, "flattened": False,
            "naked_size": naked, "resting_order": rest_ack.get("order_id")})
        logger.error("UNWIND INCOMPLETE — naked PM %.2f shares; resting reverse placed", naked)
        return {"flattened": False, "cost": None, "close": close}

    @staticmethod
    def _unwind_cost(pm_side: str, open_px: float, close_px: float, size: float) -> float:
        """Signed £-equiv cost of a flatten (positive = loss)."""
        if pm_side == "buy":          # bought at open, sold to close
            return round((open_px - close_px) * size, 4)
        return round((close_px - open_px) * size, 4)  # sold at open, bought to close

    def _record_unwound(self, trade_id, gap_ms, t0, uw: dict, bf, bf_rtt) -> None:
        self.store.update_live_trade(
            trade_id, pair_status="unwound", pm_to_bf_gap_ms=gap_ms,
            unwind_cost=uw.get("cost"),
            bf_filled_stake=(bf.matched_stake if bf else 0.0),
            bf_rtt_ms=bf_rtt, total_ms=(time.perf_counter() - t0) * 1e3)
        logger.warning("ARB STATE C — unwound (flattened=%s, cost=%s)",
                       uw.get("flattened"), uw.get("cost"))

    def _capital_ok(self, shares: float) -> bool:
        day = datetime.now(timezone.utc).date().isoformat()
        if day != self._capital_day:
            self._capital_day, self._capital_used = day, 0.0
        if self._capital_used + shares > self.max_daily_capital:
            return False
        self._capital_used += shares
        return True

    def _record_shadow(self, s, key: tuple, side: str, price: float,
                       shares: float, rtt_ms: float, h: Hops) -> None:
        now = time.time()
        order_id = self.store.save_shadow_order({
            "decision_ts": datetime.now(timezone.utc).isoformat(),
            "platform": key[0], "market_id": key[1], "outcome_id": key[2],
            "betfair_market_id": s.betfair_market_id,
            "outcome_name": s.outcome_name, "side": side, "price": price,
            "size": shares, "rtt_ms": rtt_ms, "decide_us": h.us("decision"),
        })
        self._pending.setdefault(key, []).append(PendingShadow(
            order_id=order_id, stream_key=key, side=side, price=price,
            decision_ts=now, arrival_ts=now + rtt_ms / 1000.0,
        ))

    # ---------- tick entry (shadow resolution) ----------

    def on_tick(self, tick: Tick) -> None:
        key = (tick.platform.value, tick.market_id, tick.outcome_id)
        pend = self._pending.get(key)
        now = time.time()
        if pend:
            q = tick.quote
            keep = []
            for p in pend:
                if now >= p.arrival_ts:
                    self.store.resolve_shadow_order(p.order_id, p.executable, "post_rtt_tick")
                    continue
                ok = (q.ask <= p.price + 1e-9 and q.ask_size > 0) if p.side == "buy" \
                    else (q.bid >= p.price - 1e-9 and q.bid_size > 0)
                if not ok:
                    self.store.resolve_shadow_order(p.order_id, False, "killed_early")
                    continue
                p.executable = ok
                keep.append(p)
            if keep:
                self._pending[key] = keep
            else:
                del self._pending[key]
        # lazily time out stale entries across all streams (cheap: dict scan
        # only when the tick volume is low anyway)
        if int(now) % 30 == 0:
            self._timeout_stale(now)

    def _timeout_stale(self, now: float) -> None:
        for key in list(self._pending):
            keep = []
            for p in self._pending[key]:
                if now - p.decision_ts > SHADOW_TIMEOUT_S:
                    self.store.resolve_shadow_order(
                        p.order_id, p.executable, "timeout_unchanged")
                else:
                    keep.append(p)
            if keep:
                self._pending[key] = keep
            else:
                del self._pending[key]
