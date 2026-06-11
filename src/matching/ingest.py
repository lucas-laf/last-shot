"""Ingest reviewed verdicts: `python -m src.matching.ingest verdicts.json`

Verdict entry format (one per candidate pair):
{
  "bf_market_id": "1.123", "pm_market_id": "0xabc",
  "is_match": true,
  "betfair_selection_id": "47972",   # runner equivalent to the PM YES outcome
  "confidence": 0.95,
  "resolution_risk": false,
  "reason": "same fixture, both settle on regulation time"
}

Verdicts come from Claude reviewing the export in-session; entries are
validated the same way the API matcher's output was (unknown selection ids
are rejected). Matches land as APPROVED (reviewer == the human-in-the-loop's
agent); resolution_risk=true demotes to NEEDS_REVIEW for the user.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone

from ..models import (
    MatchedPair, MatchStatus, MatchVerdict, OutcomeLink, Platform,
)
from ..settings import load_settings
from ..storage import Store

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("verdicts", help="path to verdicts JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_settings()
    store = Store(cfg["data_dir"])
    bf_markets = {m.market_id: m for m in store.get_markets(Platform.BETFAIR, active_only=False)}
    pm_markets = {m.market_id: m for m in store.get_markets(Platform.POLYMARKET, active_only=False)}

    entries = json.loads(open(args.verdicts).read())
    n_match = n_reject = n_review = n_bad = 0
    for e in entries:
        bf = bf_markets.get(e["bf_market_id"])
        pm = pm_markets.get(e["pm_market_id"])
        if not bf or not pm:
            logger.warning("unknown market ids: %s / %s", e["bf_market_id"], e["pm_market_id"])
            n_bad += 1
            continue

        verdict = MatchVerdict(
            is_match=bool(e.get("is_match")),
            confidence=float(e.get("confidence") or 0),
            resolution_risk=bool(e.get("resolution_risk", True)),
            reason=str(e.get("reason") or ""),
        )
        if verdict.is_match:
            sel = str(e.get("betfair_selection_id") or "")
            runner = next((o for o in bf.outcomes if o.outcome_id == sel), None)
            if not runner or not pm.outcomes:
                logger.warning("bad selection id %r for %s — rejecting", sel, bf.event_name)
                verdict.is_match = False
                verdict.reason += " (invalid selection id at ingest)"
                n_bad += 1
            else:
                verdict.outcome_mapping = [OutcomeLink(
                    betfair_selection_id=sel,
                    polymarket_token_id=pm.outcomes[0].outcome_id,
                    name=runner.name,
                )]

        if not verdict.is_match:
            status = MatchStatus.REJECTED
            n_reject += 1
        elif verdict.resolution_risk:
            status = MatchStatus.NEEDS_REVIEW
            n_review += 1
        else:
            status = MatchStatus.APPROVED
            n_match += 1

        store.save_verdict(MatchedPair(
            betfair_market_id=e["bf_market_id"],
            polymarket_market_id=e["pm_market_id"],
            verdict=verdict,
            status=status,
            matched_at=datetime.now(timezone.utc),
        ))
    logger.info("Ingested %d verdicts: %d approved, %d needs-review, %d rejected (%d invalid)",
                len(entries), n_match, n_review, n_reject, n_bad)


if __name__ == "__main__":
    main()
