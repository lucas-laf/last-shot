"""Stage A: cheap local prefilter producing candidate pairs for the LLM.

Pairs must share a category bucket, have compatible start times, and clear a
fuzzy text-similarity floor. Recall matters more than precision here — the LLM
is the precision stage — but the cross product (500 x 38k) must come down to
a few candidates per Betfair market.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from rapidfuzz import fuzz

from ..models import Market

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    betfair: Market
    polymarket: Market
    fuzz_score: float


def _text(m: Market) -> str:
    return f"{m.event_name} {m.market_name}".lower()


# Polymarket game markets carry sportsMarketType ('moneyline', 'spread',
# 'totals', props...); futures/politics markets leave it empty.
_H2H_BETFAIR = {"MATCH_ODDS", "MONEYLINE"}


def _type_compatible(bf: Market, pm: Market) -> bool:
    if bf.market_type in _H2H_BETFAIR:
        # head-to-head only pairs with game moneylines, never props/spreads/futures
        return pm.market_type == "moneyline"
    # outrights/winner/politics only pair with non-game markets
    return not pm.market_type


def _time_compatible(bf: Market, pm: Market, window: timedelta) -> bool:
    # The start-time window only means something for scheduled games. For
    # outrights/politics, Betfair's "start" is a settlement horizon years out
    # and Polymarket's startDate is the listing-creation date — comparing them
    # rejects valid pairs. The reviewer sees the dates either way.
    if pm.market_type != "moneyline":
        return True
    if not bf.start_time or not pm.start_time:
        return True
    return abs(bf.start_time - pm.start_time) <= window


def generate(
    betfair_markets: list[Market],
    polymarket_markets: list[Market],
    min_fuzz: float = 60.0,
    time_window_hours: float = 12.0,
    max_per_market: int = 5,
    max_per_outright: int = 40,
) -> list[Candidate]:
    window = timedelta(hours=time_window_hours)
    by_category: dict[str, list[Market]] = {}
    for pm in polymarket_markets:
        by_category.setdefault(pm.category, []).append(pm)

    out: list[Candidate] = []
    for bf in betfair_markets:
        pool = by_category.get(bf.category, [])
        bf_text = _text(bf)
        scored = []
        for pm in pool:
            if not _type_compatible(bf, pm):
                continue
            if not _time_compatible(bf, pm, window):
                continue
            score = fuzz.token_set_ratio(bf_text, _text(pm))
            if score >= min_fuzz:
                scored.append((score, pm))
        scored.sort(key=lambda s: -s[0])
        # A game maps to a handful of moneyline markets, but an outright with N
        # runners legitimately pairs with up to N one-per-candidate PM markets.
        cap = max_per_market if bf.market_type in _H2H_BETFAIR else max_per_outright
        out.extend(
            Candidate(betfair=bf, polymarket=pm, fuzz_score=score)
            for score, pm in scored[:cap]
        )
    logger.info(
        "Candidates: %d betfair x %d polymarket -> %d pairs",
        len(betfair_markets), len(polymarket_markets), len(out),
    )
    return out
