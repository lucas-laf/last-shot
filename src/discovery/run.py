"""Run one discovery pass over both platforms: `python -m src.discovery.run`."""
from __future__ import annotations

import argparse
import logging

from ..betfair_client import app_key_is_delayed, make_client
from ..models import Platform
from ..settings import load_settings
from ..storage import Store
from . import betfair as bf_discovery
from . import polymarket as pm_discovery

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-betfair", action="store_true")
    parser.add_argument("--skip-polymarket", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_settings()
    store = Store(cfg["data_dir"])
    d = cfg["discovery"]

    if not args.skip_polymarket:
        pm = pm_discovery.scan(d["polymarket_page_size"], d["polymarket_max_pages"])
        store.upsert_markets(pm)
        stale = store.mark_unseen_inactive(Platform.POLYMARKET, {m.market_id for m in pm})
        logger.info("Polymarket: stored %d markets (%d marked inactive)", len(pm), stale)

    if not args.skip_betfair:
        client = make_client(cfg)
        delayed = app_key_is_delayed(client)
        logger.info("Betfair app key delayed=%s", delayed)
        bf = bf_discovery.scan(
            client,
            d["betfair_event_types"],
            d["betfair_market_types"],
            d["betfair_max_results"],
        )
        store.upsert_markets(bf)
        stale = store.mark_unseen_inactive(Platform.BETFAIR, {m.market_id for m in bf})
        logger.info("Betfair: stored %d markets (%d marked inactive)", len(bf), stale)


if __name__ == "__main__":
    main()
