"""Betfair market discovery via listEventTypes + listMarketCatalogue.

Works on a delayed app key (catalogue data is not price data).
"""
from __future__ import annotations

import logging
from typing import Any

from betfairlightweight import APIClient, filters

from ..categories import normalize_category
from ..models import Market, Outcome, Platform

logger = logging.getLogger(__name__)

CATALOGUE_PROJECTION = [
    "EVENT", "EVENT_TYPE", "COMPETITION", "MARKET_START_TIME",
    "MARKET_DESCRIPTION", "RUNNER_DESCRIPTION",
]


def _resolve_event_type_ids(client: APIClient, names: list[str]) -> dict[str, str]:
    """Map configured event type names -> ids, warning on misses."""
    res = client.betting.list_event_types(filter=filters.market_filter())
    by_name = {r.event_type.name.lower(): r.event_type.id for r in res}
    ids = {}
    for name in names:
        et_id = by_name.get(name.lower())
        if et_id:
            ids[name] = et_id
        else:
            logger.warning("Betfair event type %r not found (have: %s)",
                           name, sorted(by_name))
    return ids


def _to_market(cat: Any, event_type_name: str) -> Market:
    event_name = cat.event.name if cat.event else ""
    competition = cat.competition.name if cat.competition else ""
    desc = cat.description
    return Market(
        platform=Platform.BETFAIR,
        market_id=cat.market_id,
        event_name=event_name,
        market_name=cat.market_name,
        category=normalize_category(event_type_name, competition, event_name),
        raw_category=f"{event_type_name}/{competition}"[:200],
        market_type=desc.market_type if desc else "",
        outcomes=[
            Outcome(outcome_id=str(r.selection_id), name=r.runner_name)
            for r in (cat.runners or [])
        ],
        start_time=cat.market_start_time,
        resolution_text=(
            f"market_type={desc.market_type}; rules={(desc.rules or '')[:3500]}"
            if desc else ""
        ),
        liquidity=float(cat.total_matched or 0),
        volume=float(cat.total_matched or 0),
        active=True,
    )


def scan(
    client: APIClient,
    event_type_names: list[str],
    sport_market_types: list[str],
    max_results: int = 1000,
) -> list[Market]:
    markets: list[Market] = []
    type_ids = _resolve_event_type_ids(client, event_type_names)
    for name, et_id in type_ids.items():
        # Politics has heterogeneous market types; sports are filtered to the
        # head-to-head/outright types we can sensibly match.
        market_types = None if name.lower() == "politics" else sport_market_types
        f = filters.market_filter(
            event_type_ids=[et_id],
            market_type_codes=market_types,
        )
        cats = client.betting.list_market_catalogue(
            filter=f,
            market_projection=CATALOGUE_PROJECTION,
            sort="MAXIMUM_TRADED",
            max_results=max_results,
        )
        logger.info("Betfair %s: %d markets", name, len(cats))
        markets.extend(_to_market(c, name) for c in cats)
    return markets
