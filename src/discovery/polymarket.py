"""Polymarket market discovery via the public Gamma API (no auth)."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Iterator

import httpx

from ..categories import normalize_category
from ..models import Market, Outcome, Platform

logger = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_events(page_size: int, max_pages: int) -> Iterator[dict[str, Any]]:
    with httpx.Client(timeout=30) as http:
        for page in range(max_pages):
            r = http.get(
                f"{GAMMA}/events",
                params={
                    "closed": "false",
                    "active": "true",
                    "limit": page_size,
                    "offset": page * page_size,
                    "order": "volume",
                    "ascending": "false",
                },
            )
            r.raise_for_status()
            events = r.json()
            if not events:
                return
            yield from events
            if len(events) < page_size:
                return


def _market_outcomes(m: dict[str, Any]) -> list[Outcome] | None:
    """Each Polymarket market is a binary YES/NO token pair."""
    try:
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
        names = json.loads(m.get("outcomes") or "[]")
    except json.JSONDecodeError:
        return None
    if len(token_ids) != 2 or len(names) != 2:
        return None
    if names[0].lower() == "yes":
        # Binary market: groupItemTitle carries the candidate/team name in
        # neg-risk events ("Uzbekistan"), else the question describes the bet.
        label = m.get("groupItemTitle") or m.get("question") or names[0]
    else:
        # Game market: outcomes are the two sides ([team1, team2]); token0 is
        # a bet on names[0] specifically, so that must be the label.
        label = names[0]
    return [Outcome(outcome_id=token_ids[0], name=label, no_token_id=token_ids[1])]


def _taker_fee_rate(m: dict[str, Any]) -> float:
    """Effective taker fee rate. Fee charged is ~ rate * min(p, 1-p) per share.

    feeSchedule.rate (e.g. 0.03 sports / 0.04 politics, takerOnly) is the live
    number; takerBaseFee is a bps base that overstates the real charge.
    """
    if not m.get("feesEnabled"):
        return 0.0
    sched = m.get("feeSchedule") or {}
    if sched.get("rate"):
        return float(sched["rate"])
    return float(m.get("takerBaseFee") or 0) / 10000.0


def scan(page_size: int = 100, max_pages: int = 60) -> list[Market]:
    markets: list[Market] = []
    n_events = 0
    for ev in _iter_events(page_size, max_pages):
        n_events += 1
        tag_labels = [t.get("label", "") for t in ev.get("tags") or []]
        category = normalize_category(ev.get("title", ""), *tag_labels)
        start = _parse_dt(ev.get("startDate")) or _parse_dt(ev.get("endDate"))
        for m in ev.get("markets") or []:
            if m.get("closed") or not m.get("active"):
                continue
            outcomes = _market_outcomes(m)
            if not outcomes:
                continue
            markets.append(
                Market(
                    platform=Platform.POLYMARKET,
                    market_id=m.get("conditionId") or m.get("id"),
                    event_name=ev.get("title", ""),
                    market_name=m.get("question", ""),
                    category=category,
                    raw_category=",".join(tag_labels)[:200],
                    market_type=m.get("sportsMarketType") or "",
                    outcomes=outcomes,
                    start_time=_parse_dt(m.get("gameStartTime")) or start,
                    resolution_text=(m.get("description") or "")[:4000],
                    liquidity=float(m.get("liquidityNum") or 0),
                    volume=float(m.get("volumeNum") or 0),
                    taker_fee=_taker_fee_rate(m),
                    active=True,
                )
            )
    logger.info("Polymarket: %d events -> %d binary markets", n_events, len(markets))
    return markets
