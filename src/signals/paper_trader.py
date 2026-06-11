"""Persist signals as hypothetical trades with full book snapshots."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Callable

from ..models import PaperTrade, Platform
from ..storage import Store
from .engine import Signal

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(
        self,
        store: Store,
        max_stake: float,
        betfair_depth: Callable[[str, str], dict],
        polymarket_depth: Callable[[str], dict],
    ):
        self.store = store
        self.max_stake = max_stake
        self.betfair_depth = betfair_depth
        self.polymarket_depth = polymarket_depth

    def on_signal(self, sig: Signal) -> None:
        s = sig.state
        bf_book = self.betfair_depth(s.betfair_market_id, s.betfair_selection_id)
        pm_book = self.polymarket_depth(s.polymarket_token_id)

        # Cap the hypothetical stake at what was actually displayed at the touch.
        displayed = self._displayed_size(sig, bf_book, pm_book)
        stake = min(self.max_stake, displayed) if displayed > 0 else 0.0
        if stake <= 0:
            return

        self.store.save_paper_trade(PaperTrade(
            ts=datetime.now(timezone.utc),
            signal_type=sig.signal_type,
            betfair_market_id=s.betfair_market_id,
            polymarket_market_id=s.polymarket_market_id,
            outcome_name=s.outcome_name,
            bet_platform=sig.bet_platform,
            side=sig.side,
            entry_prob=sig.entry_prob,
            reference_prob=sig.reference_prob,
            edge_after_fees=sig.edge_after_fees,
            stake=stake,
            betfair_book=json.dumps(bf_book),
            polymarket_book=json.dumps(pm_book),
        ))

    @staticmethod
    def _displayed_size(sig: Signal, bf_book: dict, pm_book: dict) -> float:
        """Shares available at the touch on the platform being bet."""
        s = sig.state
        if sig.bet_platform == Platform.BETFAIR:
            quote = s.bf
        else:
            quote = s.pm
        if not quote:
            return 0.0
        return quote.ask_size if sig.side == "buy" else quote.bid_size
