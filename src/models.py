"""Common schema both platforms are normalized into."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Platform(str, Enum):
    POLYMARKET = "polymarket"
    BETFAIR = "betfair"


class Outcome(BaseModel):
    """One tradeable outcome: a Betfair runner or a Polymarket YES token."""
    outcome_id: str          # betfair selectionId or polymarket clob token id
    name: str
    # polymarket only: the paired NO token, if known
    no_token_id: Optional[str] = None


class Market(BaseModel):
    platform: Platform
    market_id: str           # betfair marketId or polymarket conditionId
    event_name: str          # e.g. "Arsenal v Chelsea" / "Will Arsenal win the EPL?"
    market_name: str         # e.g. "Match Odds" / question text
    category: str            # normalized bucket: soccer, basketball, politics, ...
    raw_category: str = ""   # platform's own label, for debugging
    market_type: str = ""    # betfair marketType (MATCH_ODDS...) / polymarket sportsMarketType (moneyline...)
    outcomes: list[Outcome] = Field(default_factory=list)
    start_time: Optional[datetime] = None
    resolution_text: str = ""    # rules/description used by the LLM matcher
    liquidity: float = 0.0       # total matched (betfair) / liquidity num (polymarket)
    volume: float = 0.0
    taker_fee: float = 0.0       # polymarket taker_base_fee, if any
    active: bool = True


class OutcomeLink(BaseModel):
    betfair_selection_id: str
    polymarket_token_id: str
    name: str = ""           # human label, e.g. "Arsenal"


class MatchVerdict(BaseModel):
    """Structured output of the LLM matcher for one candidate pair."""
    is_match: bool
    confidence: float = 0.0
    outcome_mapping: list[OutcomeLink] = Field(default_factory=list)
    resolution_risk: bool = True
    reason: str = ""


class MatchStatus(str, Enum):
    AUTO_ACCEPTED = "auto_accepted"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class MatchedPair(BaseModel):
    betfair_market_id: str
    polymarket_market_id: str
    verdict: MatchVerdict
    status: MatchStatus
    matched_at: datetime


class Quote(BaseModel):
    """Best executable prices for one outcome, as implied probabilities."""
    bid: float = 0.0   # prob you can SELL at (betfair lay side / polymarket best bid)
    ask: float = 1.0   # prob you can BUY at (betfair back side / polymarket best ask)
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class Tick(BaseModel):
    ts: datetime                 # receipt time, UTC
    platform: Platform
    market_id: str
    outcome_id: str
    quote: Quote
    source_mode: str = "stream"  # "stream" | "poll" | "delayed" — affects latency conclusions


class SignalType(str, Enum):
    CONVERGENCE = "convergence"  # bet the lagging side toward the reference price
    LOCK_ARB = "lock_arb"        # opposing sides sum < 1 net of fees


class PaperTrade(BaseModel):
    ts: datetime
    signal_type: SignalType
    betfair_market_id: str
    polymarket_market_id: str
    outcome_name: str
    bet_platform: Platform           # where the hypothetical bet is placed
    side: str                        # "buy" (back) or "sell" (lay)
    entry_prob: float                # executable price actually crossed
    reference_prob: float            # what the other platform implied
    edge_after_fees: float
    stake: float
    betfair_book: str = "{}"         # JSON top-N book snapshots at signal time
    polymarket_book: str = "{}"
    settled: bool = False
    won: Optional[bool] = None
    pnl: Optional[float] = None
