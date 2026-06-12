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
    _pending: dict[tuple, list] = field(default_factory=dict)
    _arbs_by_outcome: dict[tuple, int] = field(default_factory=dict)
    _capital_day: str = ""
    _capital_used: float = 0.0

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
        else:
            buy_key = (Platform.POLYMARKET.value, s.polymarket_market_id, s.polymarket_token_id)
            sell_key = (Platform.BETFAIR.value, s.betfair_market_id, s.betfair_selection_id)
            buy_price, buy_size = s.pm.ask, s.pm.ask_size
            sell_price, sell_size = s.bf.bid, s.bf.bid_size

        shares = min(buy_size, sell_size, self.max_shares_per_leg)
        if shares <= 0:
            return
        h.mark("sized")

        outcome = (s.betfair_market_id, s.betfair_selection_id)
        if self._arbs_by_outcome.get(outcome, 0) >= self.max_arbs_per_outcome:
            return
        self._arbs_by_outcome[outcome] = self._arbs_by_outcome.get(outcome, 0) + 1

        category = self.categories.get(s.betfair_market_id, "?")
        go_live = (self.armed and category in self.live_categories
                   and self._capital_ok(shares))
        h.mark("decision")

        for key, side, price in ((buy_key, "buy", buy_price),
                                 (sell_key, "sell", sell_price)):
            rtt = (self.rtt.betfair_rtt_ms() if key[0] == "betfair"
                   else self.rtt.polymarket_rtt_ms())
            self._record_shadow(s, key, side, price, shares, rtt, h)
        h.mark("shadow_recorded")

        if go_live:
            # Live path is wired but gated: PM leg first (the stale side),
            # then the Betfair hedge. Full leg-risk handling (unwind on a
            # one-legged fill) lands with Phase B before arming.
            logger.warning("ARMED arb suppressed pending Phase B leg-risk handling: %s",
                           s.outcome_name)

        self.store.save_exec_event("arb_decision", {
            "outcome": s.outcome_name, "category": category, "shares": shares,
            "edge": sig.edge_after_fees, "armed": go_live, "hops_us": h.as_dict(),
        })

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
