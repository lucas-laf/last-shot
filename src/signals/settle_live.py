"""Settle LIVE arbs against real results, with void detection:
`python -m src.signals.settle_live`.

Unlike settle.py (paper trades), this resolves the *real* positions in the
live_trades table and flags resolution divergence — the case where one venue
pays and the other VOIDS (a tennis walkover/abandonment is the headline risk).
Each leg resolves to won | lost | void | pending; an arb settles only once both
legs are non-pending (unwound arbs settle immediately at their flatten cost).

PnL is nominal GBP≈USD (FX ignored, as in the paper PnL). divergence=1 means the
two venues disagreed on the real-world outcome OR exactly one leg voided.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from ..betfair_client import make_client
from ..settings import load_settings
from ..storage import Store

logger = logging.getLogger(__name__)
GAMMA = "https://gamma-api.polymarket.com"


def fetch_pm_results(market_ids: list[str]) -> dict[str, str]:
    """conditionId -> 'yes' | 'no' | 'void' for resolved markets (else absent)."""
    out: dict[str, str] = {}
    with httpx.Client(timeout=30) as http:
        for cid in market_ids:
            try:
                r = http.get(f"{GAMMA}/markets",
                             params={"condition_ids": cid, "closed": "true"})
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001
                logger.warning("pm result fetch failed for %s: %s", cid[:12], e)
                continue
            for m in r.json():
                if not m.get("closed"):
                    continue
                try:
                    prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                except (ValueError, json.JSONDecodeError):
                    continue
                if len(prices) != 2:
                    continue
                if prices[0] == 1.0:
                    out[cid] = "yes"
                elif prices[0] == 0.0:
                    out[cid] = "no"
                else:
                    out[cid] = "void"   # 0.5/0.5 (or other) = no clean resolution
    return out


def fetch_bf_status(client, market_ids: list[str]) -> dict[str, dict]:
    """market_id -> {'closed': bool, 'winner_exists': bool, sel_id: 'WINNER'|'LOSER'|...}."""
    out: dict[str, dict] = {}
    for i in range(0, len(market_ids), 40):
        for book in client.betting.list_market_book(market_ids=market_ids[i:i + 40]):
            closed = book.status == "CLOSED"
            runners = {str(r.selection_id): r.status for r in (book.runners or [])}
            out[book.market_id] = {
                "closed": closed,
                "winner_exists": any(s == "WINNER" for s in runners.values()),
                **runners,
            }
    return out


def _pm_leg(row, pm_res: str | None):
    """(result, pnl, yes_won) for the Polymarket leg. result in won|lost|void|pending."""
    size = row["pm_filled_size"] or 0.0
    cost = size * (row["pm_filled_price"] or 0.0)
    if pm_res is None:
        return "pending", None, None
    if pm_res == "void":
        return "void", 0.0, None          # refund: payout == cost
    yes_won = pm_res == "yes"
    token_won = (not yes_won) if row["pm_is_short"] else yes_won
    pnl = (size - cost) if token_won else -cost
    return ("won" if token_won else "lost"), round(pnl, 4), yes_won


def _bf_leg(row, st: dict | None):
    """(result, x_won) for the Betfair leg. x_won = did the backed outcome win
    per Betfair; result in won|lost|void|pending."""
    if st is None or not st.get("closed"):
        return "pending", None
    sel = row["bf_selection_id"]
    if not sel:
        return "pending", None     # pre-migration row: can't resolve the leg
    sel_status = st.get(sel)
    # Voided market (walkover/abandonment) or our runner removed -> void.
    if not st.get("winner_exists") or sel_status in ("REMOVED", "REMOVED_VACANT", None):
        return "void", None
    return ("won" if sel_status == "WINNER" else "lost"), (sel_status == "WINNER")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_settings()
    store = Store(cfg["data_dir"])
    store._conn.row_factory = __import__("sqlite3").Row
    rows = store._conn.execute(
        """SELECT * FROM live_trades
           WHERE settled IS NOT 1 AND pair_status IN ('locked','unwound')"""
    ).fetchall()
    if not rows:
        logger.info("no unsettled live arbs")
        return

    # unwound arbs are already closed at flatten time
    locked = [r for r in rows if r["pair_status"] == "locked"]
    pm_res = fetch_pm_results(sorted({r["polymarket_market_id"] for r in locked})) if locked else {}
    bf_st = fetch_bf_status(make_client(cfg),
                            sorted({r["betfair_market_id"] for r in locked})) if locked else {}

    n_settled = n_div = 0
    for r in rows:
        if r["pair_status"] == "unwound":
            store.update_live_trade(r["id"], settled=1, pm_result="unwound",
                                    bf_result="unwound",
                                    realized_pnl=-(r["unwind_cost"] or 0.0),
                                    divergence=0, settled_ts=datetime.now(timezone.utc).isoformat())
            n_settled += 1
            continue

        pm_result, pm_pnl, yes_won = _pm_leg(r, pm_res.get(r["polymarket_market_id"]))
        bf_result, x_won_bf = _bf_leg(r, bf_st.get(r["betfair_market_id"]))
        if pm_result == "pending" or bf_result == "pending":
            continue  # not both resolved yet

        # Betfair leg PnL from Betfair's OWN outcome (not assumed from PM).
        stake, p = r["bf_filled_stake"] or 0.0, r["bf_filled_price"] or 0.0
        odds = 1.0 / p if p > 0 else 0.0
        bf_pnl = 0.0
        if bf_result != "void" and x_won_bf is not None and odds > 0:
            if r["bf_side"] == "buy":          # BACK the outcome
                bf_pnl = stake * (odds - 1) if x_won_bf else -stake
            else:                               # LAY the outcome
                bf_pnl = stake if not x_won_bf else -stake * (odds - 1)

        pm_void, bf_void = pm_result == "void", bf_result == "void"
        # divergence: exactly one leg voided, OR both resolved but the venues
        # disagree on whether the outcome happened (PM yes_won vs Betfair x_won).
        divergence = int((pm_void != bf_void)
                         or (not pm_void and not bf_void and yes_won != x_won_bf))
        realized = round((pm_pnl or 0.0) + bf_pnl, 4)
        store.update_live_trade(
            r["id"], settled=1, pm_result=pm_result, bf_result=bf_result,
            realized_pnl=realized, divergence=divergence,
            settled_ts=datetime.now(timezone.utc).isoformat())
        n_settled += 1
        if divergence:
            n_div += 1
            logger.warning("DIVERGENCE on %s: pm=%s bf=%s realized=%.3f",
                           r["outcome_name"], pm_result, bf_result, realized)

    logger.info("Settled %d live arbs (%d divergences)", n_settled, n_div)


if __name__ == "__main__":
    main()
