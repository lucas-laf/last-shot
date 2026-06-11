"""Export unjudged candidate pairs for in-session review by Claude:
`python -m src.matching.export [-o candidates.json]`

The reviewer (Claude, orchestrating this pipeline) reads the JSON, decides
each pair, and feeds verdicts back via `python -m src.matching.ingest`.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from ..models import Platform
from ..settings import load_settings
from ..storage import Store
from . import candidates as stage_a

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--out", default="data/candidates.json")
    parser.add_argument("--rules-chars", type=int, default=350,
                        help="how much resolution text to include per side")
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

    rows = []
    for i, c in enumerate(new):
        rows.append({
            "idx": i,
            "bf_market_id": c.betfair.market_id,
            "pm_market_id": c.polymarket.market_id,
            "fuzz": round(c.fuzz_score, 1),
            "category": c.betfair.category,
            "bf_event": c.betfair.event_name,
            "bf_market": c.betfair.market_name,
            "bf_type": c.betfair.market_type,
            "bf_start": str(c.betfair.start_time or ""),
            "bf_runners": {o.outcome_id: o.name for o in c.betfair.outcomes},
            "bf_rules": c.betfair.resolution_text[:args.rules_chars],
            "pm_question": c.polymarket.market_name,
            "pm_outcome": c.polymarket.outcomes[0].name if c.polymarket.outcomes else "",
            "pm_start": str(c.polymarket.start_time or ""),
            "pm_rules": c.polymarket.resolution_text[:args.rules_chars],
        })
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))
    logger.info("Wrote %d unjudged candidates to %s", len(rows), out)


if __name__ == "__main__":
    main()
