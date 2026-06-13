"""Manual first-live-order test for the Polymarket leg.

DRY-RUN BY DEFAULT. Without --arm it builds and signs a real order (resolving
the market's tick size + neg-risk flag and printing the exact OrderArgs) but
never posts it — proving the order-builder is correct without moving money.

With --arm it posts a tiny GTC limit order priced well below the best bid so
it cannot fill, prints the exchange ack (this is what catches the historical
`invalid order version` rejection), then immediately cancels it. Net effect:
the order is accepted/rejected by the CLOB and torn down without trading.

    # dry run — build + sign + show, no network order:
    .venv/bin/python -m scripts.place_test_order

    # live acceptance test (tiny resting order, auto-cancelled):
    EXECUTOR_ARMED=1 .venv/bin/python -m scripts.place_test_order --arm

Defaults to a soccer binary (non-neg-risk) market per the resume plan; pass
--token to target a specific YES token, or --category to change the bucket.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math

import httpx

from src.execution.polymarket_executor import PolymarketExecutor
from src.models import Platform
from src.settings import load_settings
from src.storage import Store

CLOB = "https://clob.polymarket.com"


def _clob_meta(token_id: str) -> tuple[bool, str, dict]:
    """(neg_risk, tick_size, best book levels) straight from the CLOB."""
    nr = httpx.get(f"{CLOB}/neg-risk", params={"token_id": token_id}, timeout=15).json()
    ts = httpx.get(f"{CLOB}/tick-size", params={"token_id": token_id}, timeout=15).json()
    book = httpx.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=15).json()
    return bool(nr.get("neg_risk")), str(ts.get("minimum_tick_size")), book


def _best_bid(book: dict) -> float:
    bids = book.get("bids") or []
    return max((float(b["price"]) for b in bids), default=0.0)


def _pick_token(store: Store, category: str) -> tuple[str, str]:
    """First active market in `category` that is non-neg-risk with a live bid."""
    markets = store.get_markets(Platform.POLYMARKET, active_only=True)
    cands = [m for m in markets if m.category == category and m.outcomes]
    cands.sort(key=lambda m: m.volume, reverse=True)
    for m in cands:
        token = m.outcomes[0].outcome_id
        try:
            neg_risk, _, book = _clob_meta(token)
        except Exception:
            continue
        if not neg_risk and _best_bid(book) > 0:
            return token, m.market_name
    raise SystemExit(f"No non-neg-risk {category} market with a live bid found.")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="store_true",
                    help="actually post the order (then cancel). Default: dry run.")
    ap.add_argument("--token", default="", help="YES token id to target")
    ap.add_argument("--category", default="soccer", help="market bucket to pick from")
    ap.add_argument("--size", type=float, default=0.0,
                    help="shares; default sizes to ~$1.50 notional")
    ap.add_argument("--price", type=float, default=0.0,
                    help="raw price; default = half the best bid (won't fill)")
    args = ap.parse_args()

    cfg = load_settings()
    store = Store(cfg["data_dir"])

    token = args.token
    name = "(token override)"
    if not token:
        token, name = _pick_token(store, args.category)

    neg_risk, tick, book = _clob_meta(token)
    best_bid = _best_bid(book)
    price = args.price or max(2 * float(tick), best_bid * 0.5)
    size = args.size or max(5.0, math.ceil(1.50 / max(price, 0.01)))

    print(f"market   : {name}")
    print(f"token    : {token}")
    print(f"neg_risk : {neg_risk}   tick: {tick}   best_bid: {best_bid}")
    print(f"order    : BUY {size} @ {price}  (GTC resting, below bid -> no fill)")
    print(f"mode     : {'ARMED — will post then cancel' if args.arm else 'DRY RUN — build+sign only'}")

    pm = PolymarketExecutor(
        store,
        private_key=cfg["poly_private_key"],
        funder=cfg["poly_funder"],
        armed=args.arm,
        signature_type=cfg["poly_signature_type"],
    )
    order = pm.build_order(token, "buy", price, size,
                           neg_risk=neg_risk, tick_size=tick)
    ack = await pm.place(order, order_type="GTC")
    print("\nack:", json.dumps({k: v for k, v in ack.items() if k != "order"}, default=str, indent=2))

    if ack.get("sent") and ack.get("order_id"):
        cancel = await pm.cancel(ack["order_id"])
        print("cancel:", json.dumps(cancel, default=str))


if __name__ == "__main__":
    asyncio.run(main())
