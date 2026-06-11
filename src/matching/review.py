"""Approve/reject queued matches: `python -m src.matching.review`."""
from __future__ import annotations

import argparse

from ..models import MatchStatus, Platform
from ..settings import load_settings
from ..storage import Store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="list tracked pairs and exit")
    args = parser.parse_args()

    cfg = load_settings()
    store = Store(cfg["data_dir"])
    markets = {
        (m.platform, m.market_id): m
        for p in (Platform.BETFAIR, Platform.POLYMARKET)
        for m in store.get_markets(p, active_only=False)
    }

    def describe(pair) -> str:
        bf = markets.get((Platform.BETFAIR, pair.betfair_market_id))
        pm = markets.get((Platform.POLYMARKET, pair.polymarket_market_id))
        link = pair.verdict.outcome_mapping[0] if pair.verdict.outcome_mapping else None
        return (
            f"  BF: {bf.event_name} / {bf.market_name}\n" if bf else "  BF: ?\n"
        ) + (
            f"  PM: {pm.market_name}\n" if pm else "  PM: ?\n"
        ) + (
            f"  outcome: {link.name}\n" if link else ""
        ) + (
            f"  conf={pair.verdict.confidence:.2f} resolution_risk={pair.verdict.resolution_risk}\n"
            f"  reason: {pair.verdict.reason}"
        )

    if args.list:
        tracked = store.get_pairs([MatchStatus.AUTO_ACCEPTED, MatchStatus.APPROVED])
        print(f"{len(tracked)} tracked pairs:")
        for p in tracked:
            print(f"\n[{p.status.value}]")
            print(describe(p))
        return

    queue = store.get_pairs([MatchStatus.NEEDS_REVIEW])
    print(f"{len(queue)} pairs awaiting review\n")
    for pair in queue:
        print(describe(pair))
        while True:
            ans = input("approve / reject / skip [a/r/s]? ").strip().lower()
            if ans in ("a", "r", "s"):
                break
        if ans == "a":
            store.set_pair_status(pair.betfair_market_id, pair.polymarket_market_id,
                                  MatchStatus.APPROVED)
        elif ans == "r":
            store.set_pair_status(pair.betfair_market_id, pair.polymarket_market_id,
                                  MatchStatus.REJECTED)
        print()


if __name__ == "__main__":
    main()
