"""Settle paper trades against real results: `python -m src.signals.settle`.

Resolution source follows the platform the hypothetical bet was placed on:
Betfair runner status for Betfair legs, Polymarket outcome prices for
Polymarket legs — so resolution-rule mismatches show up as real PnL, exactly
like they would with money on.

PnL model (stake = number of shares, $1 payout per share):
  buy at p:  win -> (1 - p) * stake, lose -> -p * stake
  sell at p: win -> -(1 - p) * stake, lose -> p * stake
entry_prob is already net of fees (see signals.fees).
"""
from __future__ import annotations

import json
import logging

import httpx

from ..betfair_client import make_client
from ..models import Platform
from ..settings import load_settings
from ..storage import Store

logger = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"


def fetch_betfair_winners(client, market_ids: list[str]) -> dict[str, dict[str, bool]]:
    """market_id -> {selection_id: won} for CLOSED markets only."""
    out: dict[str, dict[str, bool]] = {}
    for i in range(0, len(market_ids), 40):
        chunk = market_ids[i:i + 40]
        for book in client.betting.list_market_book(market_ids=chunk):
            if book.status != "CLOSED":
                continue
            out[book.market_id] = {
                str(r.selection_id): r.status == "WINNER"
                for r in book.runners or []
            }
    return out


def fetch_polymarket_results(market_ids: list[str]) -> dict[str, bool]:
    """conditionId -> YES won, for resolved markets only."""
    out: dict[str, bool] = {}
    with httpx.Client(timeout=30) as http:
        for cid in market_ids:
            # closed=true is required: Gamma silently omits resolved markets
            # from /markets responses unless explicitly asked for them.
            r = http.get(f"{GAMMA}/markets",
                         params={"condition_ids": cid, "closed": "true"})
            r.raise_for_status()
            for m in r.json():
                if not m.get("closed"):
                    continue
                try:
                    prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                except (ValueError, json.JSONDecodeError):
                    continue
                if len(prices) == 2 and (prices[0] in (0.0, 1.0)):
                    out[cid] = prices[0] == 1.0
    return out


def pnl(side: str, entry: float, stake: float, won: bool) -> float:
    if side == "buy":
        return (1.0 - entry) * stake if won else -entry * stake
    return -(1.0 - entry) * stake if won else entry * stake


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_settings()
    store = Store(cfg["data_dir"])

    with store._conn as conn:
        rows = conn.execute(
            """SELECT id, betfair_market_id, polymarket_market_id, outcome_name,
                      bet_platform, side, entry_prob, stake
               FROM paper_trades WHERE settled=0"""
        ).fetchall()
    if not rows:
        logger.info("No unsettled paper trades")
        return
    logger.info("%d unsettled trades", len(rows))

    bf_ids = sorted({r[1] for r in rows if r[4] == Platform.BETFAIR.value})
    pm_ids = sorted({r[2] for r in rows if r[4] == Platform.POLYMARKET.value})
    bf_results = fetch_betfair_winners(make_client(cfg), bf_ids) if bf_ids else {}
    pm_results = fetch_polymarket_results(pm_ids) if pm_ids else {}

    # selection-id lookup for betfair legs
    pairs = store.get_pairs()
    sel_by_pair = {
        (p.betfair_market_id, p.polymarket_market_id):
            p.verdict.outcome_mapping[0].betfair_selection_id
        for p in pairs if p.verdict.outcome_mapping
    }

    n = 0
    for trade_id, bf_id, pm_id, name, platform, side, entry, stake in rows:
        won: bool | None = None
        if platform == Platform.BETFAIR.value:
            sel = sel_by_pair.get((bf_id, pm_id))
            market = bf_results.get(bf_id)
            if market is not None and sel in market:
                won = market[sel]
        else:
            won = pm_results.get(pm_id)
        if won is None:
            continue
        with store._conn as conn:
            conn.execute(
                "UPDATE paper_trades SET settled=1, won=?, pnl=? WHERE id=?",
                (int(won), pnl(side, entry, stake, won), trade_id),
            )
        n += 1
    logger.info("Settled %d trades", n)


if __name__ == "__main__":
    main()
