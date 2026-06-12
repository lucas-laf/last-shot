"""Divergence detection over per-outcome pair state.

Two signal types, both required to clear costs plus min_edge:

- LOCK_ARB: buy on one platform and sell on the other at net prices that sum
  to a guaranteed profit regardless of outcome (held to settlement).
- CONVERGENCE: the executable price on one platform deviates from the
  reference price (the deeper side's mid) — a bet that prices reconverge.
  Inventory risk; logged so the data can show whether it pays.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ..models import Platform, Quote, SignalType
from . import fees

logger = logging.getLogger(__name__)


@dataclass
class PairState:
    """One tracked outcome: a Betfair runner <-> a Polymarket YES token."""
    betfair_market_id: str
    betfair_selection_id: str
    polymarket_market_id: str
    polymarket_token_id: str
    outcome_name: str
    pm_taker_rate: float = 0.0
    bf_liquidity: float = 0.0
    pm_liquidity: float = 0.0
    bf: Optional[Quote] = None
    pm: Optional[Quote] = None
    bf_ts: Optional[datetime] = None
    pm_ts: Optional[datetime] = None
    last_fired: dict = field(default_factory=dict)  # signal key -> datetime


@dataclass
class Signal:
    signal_type: SignalType
    state: PairState
    bet_platform: Platform
    side: str            # "buy" | "sell"
    entry_prob: float    # net executable price crossed
    reference_prob: float
    edge_after_fees: float


class SignalEngine:
    def __init__(
        self,
        commission: float,
        min_edge: float,
        cooldown_seconds: float = 120.0,
        stale_after_seconds: float = 30.0,
        on_signal: Callable[[Signal], None] | None = None,
        convergence_ref: str = "deeper",  # "deeper" | "betfair" | "polymarket"
        max_ref_spread: float | None = None,
        convergence_enabled: bool = True,
    ):
        self.commission = commission
        self.min_edge = min_edge
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self.stale_after = timedelta(seconds=stale_after_seconds)
        self.on_signal = on_signal
        if convergence_ref not in ("deeper", "betfair", "polymarket"):
            raise ValueError(f"bad convergence_ref: {convergence_ref!r}")
        self.convergence_ref = convergence_ref
        self.max_ref_spread = max_ref_spread
        # Retired 2026-06-12: no convergence variant survived out-of-sample
        # testing (86 events, t=-0.58); replay.py can resurrect it from ticks.
        self.convergence_enabled = convergence_enabled

    def evaluate(self, s: PairState, now: datetime | None = None) -> list[Signal]:
        """`now` overrides wall-clock time so recorded ticks can be replayed."""
        if now is None:
            now = datetime.now(timezone.utc)
        if not s.bf or not s.pm or not s.bf_ts or not s.pm_ts:
            return []
        # Never trade on a stale leg: a "divergence" against a dead feed is noise.
        if now - s.bf_ts > self.stale_after or now - s.pm_ts > self.stale_after:
            return []
        if s.bf.bid <= 0 or s.pm.bid <= 0 or s.bf.ask >= 1 or s.pm.ask >= 1:
            return []

        c, r = self.commission, s.pm_taker_rate
        bf_buy = fees.betfair_buy(s.bf.ask, c)
        bf_sell = fees.betfair_sell(s.bf.bid, c)
        pm_buy = fees.polymarket_buy(s.pm.ask, r)
        pm_sell = fees.polymarket_sell(s.pm.bid, r)

        out: list[Signal] = []

        # --- lock arb: opposing sides, profit locked at settlement ---
        if (edge := bf_sell - pm_buy) >= self.min_edge:
            out += self._fire(s, SignalType.LOCK_ARB, Platform.POLYMARKET, "buy",
                              pm_buy, bf_sell, edge, now)
            out += self._fire(s, SignalType.LOCK_ARB, Platform.BETFAIR, "sell",
                              bf_sell, pm_buy, edge, now)
        if (edge := pm_sell - bf_buy) >= self.min_edge:
            out += self._fire(s, SignalType.LOCK_ARB, Platform.BETFAIR, "buy",
                              bf_buy, pm_sell, edge, now)
            out += self._fire(s, SignalType.LOCK_ARB, Platform.POLYMARKET, "sell",
                              pm_sell, bf_buy, edge, now)

        # --- convergence: the reference platform's mid is treated as truth ---
        if not self.convergence_enabled:
            for sig in out:
                if self.on_signal:
                    self.on_signal(sig)
            return out
        if self.convergence_ref == "betfair" or (
            self.convergence_ref == "deeper" and s.bf_liquidity >= s.pm_liquidity
        ):
            ref, ref_quote = s.bf.mid, s.bf
            buy_net, sell_net, bet_platform = pm_buy, pm_sell, Platform.POLYMARKET
        else:
            ref, ref_quote = s.pm.mid, s.pm
            buy_net, sell_net, bet_platform = bf_buy, bf_sell, Platform.BETFAIR
        # A mid from a wide book is an artifact, not a price — don't treat it
        # as truth.
        if self.max_ref_spread is None or ref_quote.spread <= self.max_ref_spread:
            if (edge := ref - buy_net) >= self.min_edge:
                out += self._fire(s, SignalType.CONVERGENCE, bet_platform, "buy",
                                  buy_net, ref, edge, now)
            elif (edge := sell_net - ref) >= self.min_edge:
                out += self._fire(s, SignalType.CONVERGENCE, bet_platform, "sell",
                                  sell_net, ref, edge, now)

        for sig in out:
            if self.on_signal:
                self.on_signal(sig)
        return out

    def _fire(
        self, s: PairState, sig_type: SignalType, platform: Platform, side: str,
        entry: float, ref: float, edge: float, now: datetime,
    ) -> list[Signal]:
        key = f"{sig_type.value}:{platform.value}:{side}"
        last = s.last_fired.get(key)
        if last and now - last < self.cooldown:
            return []
        s.last_fired[key] = now
        logger.info(
            "SIGNAL %s %s %s %s @%.3f ref=%.3f edge=%.3f",
            sig_type.value, s.outcome_name, platform.value, side, entry, ref, edge,
        )
        return [Signal(
            signal_type=sig_type, state=s, bet_platform=platform, side=side,
            entry_prob=entry, reference_prob=ref, edge_after_fees=edge,
        )]
