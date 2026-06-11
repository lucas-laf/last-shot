"""Convert both platforms' books into implied-probability Quotes.

Convention: a Quote is always about the SAME thing — the probability of the
outcome happening — with `ask` the price you can buy exposure at and `bid`
the price you can sell at. bid <= ask always.

Betfair: backing at decimal odds o == buying probability at 1/o, so the best
available BACK odds give the ask; the best available LAY odds give the bid.
Best back odds are always below best lay odds, hence 1/back >= 1/lay -> ask >= bid.

Polymarket: the YES token book is already denominated in probability.
"""
from __future__ import annotations

from ..models import Quote


def quote_from_betfair(
    best_back: tuple[float, float] | None,   # (decimal odds, stake size GBP)
    best_lay: tuple[float, float] | None,
) -> Quote:
    ask, ask_size = 1.0, 0.0
    bid, bid_size = 0.0, 0.0
    if best_back and best_back[0] > 1:
        odds, size = best_back
        ask = 1.0 / odds
        ask_size = size * odds          # GBP stake -> GBP payout ~ "shares"
    if best_lay and best_lay[0] > 1:
        odds, size = best_lay
        bid = 1.0 / odds
        bid_size = size * odds
    return Quote(bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)


def quote_from_polymarket_book(
    bids: dict[float, float],   # price -> size (shares), YES token
    asks: dict[float, float],
) -> Quote:
    bid = max(bids) if bids else 0.0
    ask = min(asks) if asks else 1.0
    return Quote(
        bid=bid,
        ask=ask,
        bid_size=bids.get(bid, 0.0),
        ask_size=asks.get(ask, 0.0),
    )


def overround(probs: list[float]) -> float:
    """Sum of implied probabilities across all outcomes of one market; >1 means
    the book builds in margin. Useful for sanity checks on betfair mids."""
    return sum(probs)
