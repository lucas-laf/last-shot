"""Set Polymarket CLOB **V2** exchange allowances for the funded wallet.

After the 2026-04-28 CLOB V2 migration, the V2 Exchange is a new contract, so
the USDC/CTF allowances granted during the old V1 onboarding no longer cover
live V2 orders — the first real order will fail on allowance until these are
set. See the [[polymarket-clob-v2-migration]] memory.

For a proxy wallet (signature_type 2, our setup) `update_balance_allowance` is
a gasless relayer call — Polymarket sets the approval on our behalf; we don't
sign or pay gas. It is still account-state-changing, so:

DRY-RUN BY DEFAULT — prints current balance/allowance for COLLATERAL (USDC) and
CONDITIONAL (CTF) and what it *would* set. Pass --arm to actually request the
relayer to set them.

    .venv/bin/python -m scripts.set_v2_allowances           # read-only
    .venv/bin/python -m scripts.set_v2_allowances --arm     # set allowances
"""
from __future__ import annotations

import argparse
import json

from src.settings import load_settings

CLOB_HOST = "https://clob.polymarket.com"
POLYGON = 137


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="store_true",
                    help="actually request the relayer to set allowances")
    args = ap.parse_args()

    cfg = load_settings()
    if not cfg["poly_private_key"]:
        raise SystemExit("POLY_PRIVATE_KEY not set in .env")

    # A sample CTF token id is required to read the erc1155 (CONDITIONAL)
    # allowance; the approval itself is blanket (setApprovalForAll).
    from src.models import Platform
    from src.storage import Store
    store = Store(cfg["data_dir"])
    sample_token = next((m.outcomes[0].outcome_id
                         for m in store.get_markets(Platform.POLYMARKET, active_only=True)
                         if m.outcomes), None)

    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    client = ClobClient(
        CLOB_HOST, POLYGON,
        key=cfg["poly_private_key"],
        funder=cfg["poly_funder"] or None,
        signature_type=cfg["poly_signature_type"] if cfg["poly_funder"] else None,
    )
    client.set_api_creds(client.create_or_derive_api_key())

    assets = [("USDC collateral", AssetType.COLLATERAL, None),
              ("CTF conditional", AssetType.CONDITIONAL, sample_token)]

    print(f"funder    : {cfg['poly_funder']}")
    print(f"sig_type  : {cfg['poly_signature_type']}")
    print(f"mode      : {'ARMED — will set allowances' if args.arm else 'DRY RUN — read only'}\n")

    for label, asset, token in assets:
        params = BalanceAllowanceParams(asset_type=asset, token_id=token)
        before = client.get_balance_allowance(params)
        print(f"[{label}] current: {json.dumps(before, default=str)}")
        if args.arm:
            client.update_balance_allowance(params)
            after = client.get_balance_allowance(params)
            print(f"[{label}] after  : {json.dumps(after, default=str)}")
        print()


if __name__ == "__main__":
    main()
