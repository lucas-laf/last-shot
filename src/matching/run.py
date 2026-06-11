"""Run the matching pipeline over stored markets: `python -m src.matching.run`.

Incremental: pairs that already have a verdict are never re-judged.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

from ..models import MatchedPair, MatchStatus, Platform
from ..settings import load_settings
from ..storage import Store
from . import candidates as stage_a
from .llm_matcher import LLMMatcher

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0,
                        help="judge at most N new pairs (0 = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Stage A only: print candidates, no LLM calls")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_settings()
    store = Store(cfg["data_dir"])
    mcfg = cfg["matching"]

    bf = store.get_markets(Platform.BETFAIR)
    pm = store.get_markets(Platform.POLYMARKET)
    cands = stage_a.generate(
        bf, pm,
        min_fuzz=mcfg["candidate_min_fuzz"],
        time_window_hours=mcfg["event_time_window_hours"],
    )
    new = [
        c for c in cands
        if not store.has_verdict(c.betfair.market_id, c.polymarket.market_id)
    ]
    logger.info("%d candidate pairs (%d new)", len(cands), len(new))

    if args.dry_run:
        for c in new[:40]:
            print(f"[{c.fuzz_score:5.1f}] {c.betfair.event_name} / {c.betfair.market_name}"
                  f"  <->  {c.polymarket.market_name}")
        return

    if args.limit:
        new = new[:args.limit]

    matcher = LLMMatcher(
        api_key=cfg["anthropic_api_key"],
        bulk_model=mcfg["bulk_model"],
        escalation_model=mcfg["escalation_model"],
        escalation_band=tuple(mcfg["escalation_confidence_band"]),
    )
    auto = cfg["matching"]["auto_accept_confidence"]
    n_match = n_auto = 0
    for i, c in enumerate(new, 1):
        verdict = matcher.judge(c)
        if not verdict.is_match:
            status = MatchStatus.REJECTED
        elif verdict.confidence >= auto and not verdict.resolution_risk:
            status = MatchStatus.AUTO_ACCEPTED
            n_auto += 1
        else:
            status = MatchStatus.NEEDS_REVIEW
        if verdict.is_match:
            n_match += 1
            logger.info("MATCH (%s, conf %.2f) %s <-> %s | %s",
                        status.value, verdict.confidence,
                        c.betfair.event_name, c.polymarket.market_name,
                        verdict.reason)
        store.save_verdict(MatchedPair(
            betfair_market_id=c.betfair.market_id,
            polymarket_market_id=c.polymarket.market_id,
            verdict=verdict,
            status=status,
            matched_at=datetime.now(timezone.utc),
        ))
        if i % 25 == 0:
            logger.info("judged %d/%d", i, len(new))
    logger.info("Done: %d matches (%d auto-accepted) out of %d pairs",
                n_match, n_auto, len(new))


if __name__ == "__main__":
    main()
