"""Fee-adjusted effective prices. All prices are implied probabilities.

Betfair: commission c is charged on net market winnings.
 - Backing (buying) at prob p: stake s wins s(1/p - 1)(1 - c), so the
   effective probability paid is 1 / (1 + (1/p - 1)(1 - c)) > p.
 - Laying (selling) at prob p: proceeds are cut by commission on the win
   branch; p(1 - c) is a conservative effective sell price.

Polymarket: taker fee ~ rate * min(p, 1 - p) per share (taker-only schedule).
"""
from __future__ import annotations


def betfair_buy(p: float, commission: float) -> float:
    if p <= 0 or p >= 1:
        return 1.0
    return 1.0 / (1.0 + (1.0 / p - 1.0) * (1.0 - commission))


def betfair_sell(p: float, commission: float) -> float:
    return p * (1.0 - commission)


def polymarket_buy(p: float, taker_rate: float) -> float:
    return p + taker_rate * min(p, 1.0 - p)


def polymarket_sell(p: float, taker_rate: float) -> float:
    return p - taker_rate * min(p, 1.0 - p)
