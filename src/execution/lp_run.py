"""Standalone runner for the Polymarket LP (liquidity-reward) market-making bot.

Selects mean-reverting reward markets (reusing lp_quoter.scan_candidates + allocate),
builds MarketStates, and runs the LpExecutor loops. Places NO orders unless BOTH
config `execution.armed: true` AND env `EXECUTOR_ARMED=1` (settings.py enforces this);
otherwise every order is BUILT_NOT_SENT / SHADOW.

    python -m src.execution.lp_run            # shadow/dry-run per config+env
    EXECUTOR_ARMED=1 python -m src.execution.lp_run   # live (if config armed + creds)

Capital ceiling and risk knobs come from config.yaml -> execution.lp.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import urllib.request
from datetime import datetime, timezone

from ..settings import load_settings
from ..storage import Store
from .polymarket_executor import PolymarketExecutor
from .lp_quoter import scan_candidates, allocate
from .lp_executor import LpExecutor, MarketState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("lp_run")


def _end_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _quotable(c: dict, blackout_hours: float, inv_book_frac: float) -> bool:
    """A candidate is worth funding only if it will actually quote: not within the
    blackout window of resolution, and with enough two-sided book that our min_size
    stays <= inv_book_frac of the resulting book (the same gate the executor applies)."""
    ets = _end_ts(c.get("end_date"))
    if ets is not None:
        hrs = (ets - datetime.now(timezone.utc).timestamp()) / 3600.0
        if hrs < blackout_hours:
            return False
    thin = c["min_size"] * (1 - inv_book_frac) / inv_book_frac
    return c.get("qual", 0) >= thin * 1.5   # 1.5x headroom so it quotes through book wiggle


def _to_market_state(c: dict) -> MarketState | None:
    if not (c.get("condition_id") and c.get("yes_token") and c.get("no_token")):
        return None
    return MarketState(
        condition_id=c["condition_id"], question=c["q"],
        yes_token=c["yes_token"], no_token=c["no_token"],
        tick=c["tick"], max_spread=c["max_spread"] / 100.0, min_size=c["min_size"],
        pool_day=c["pool"], qual=c["qual"], end_ts=_end_ts(c.get("end_date")),
        mid=c["mid"])


def _lp_condition_ids(store) -> set:
    """Markets we've ever placed LP fills in — used to attribute on-chain positions to
    the LP bot (vs legacy/taker holdings on the same wallet)."""
    try:
        rows = store._conn.execute(
            "SELECT DISTINCT json_extract(payload_json,'$.condition_id') "
            "FROM exec_events WHERE kind='lp_fill'").fetchall()
        return {r[0] for r in rows if r[0]}
    except Exception:
        return set()


def _fetch_lp_holdings(funder: str, lp_cids: set) -> dict:
    """Current ON-CHAIN positions, restricted to our LP markets, aggregated per
    condition_id -> {q_yes,q_no,yes_avg,no_avg,yes_token,no_token,mid,end_date,title}.
    On-chain is the source of truth (handles resolution/manual changes)."""
    if not lp_cids:
        return {}
    try:
        req = urllib.request.Request(
            f"https://data-api.polymarket.com/positions?user={funder}",
            headers={"User-Agent": "curl/8.5.0"})
        positions = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception as e:
        logger.warning("LP reconcile: positions fetch failed (%s) — treating as flat", e)
        return {}
    out: dict = {}
    for p in positions:
        cid = p.get("conditionId")
        if cid not in lp_cids or float(p.get("size", 0) or 0) < 1e-6:
            continue
        h = out.setdefault(cid, dict(q_yes=0.0, q_no=0.0, yes_avg=0.0, no_avg=0.0,
                                     yes_token="", no_token="", mid=0.5,
                                     end_date=p.get("endDate"), title=p.get("title", "")))
        cur = float(p.get("curPrice", 0) or 0)
        if p.get("outcome") == "Yes":
            h.update(q_yes=float(p["size"]), yes_avg=float(p["avgPrice"]),
                     yes_token=p["asset"], no_token=p.get("oppositeAsset", ""), mid=cur)
        else:
            h.update(q_no=float(p["size"]), no_avg=float(p["avgPrice"]),
                     no_token=p["asset"], yes_token=p.get("oppositeAsset", ""))
            if h["mid"] == 0.5:
                h["mid"] = round(1.0 - cur, 4)   # YES mid ~ 1 - NO price
    return out


def _held_only_market_state(cid: str, h: dict) -> MarketState | None:
    """A count-only MarketState for a held position in a market we're NOT quoting
    (e.g. it dropped out of rewards). max_spread=0 + pool=0 => it never quotes; it is
    only tracked (mark-to-mid) and counted toward the budget cap."""
    if not (h.get("yes_token") and h.get("no_token")):
        return None
    return MarketState(
        condition_id=cid, question=(h.get("title") or "(held)")[:60],
        yes_token=h["yes_token"], no_token=h["no_token"], tick=0.01,
        max_spread=0.0, min_size=0.0, pool_day=0.0, qual=0.0,
        end_ts=_end_ts(h.get("end_date")), mid=h.get("mid", 0.5))


async def main() -> None:
    cfg = load_settings()
    ex = cfg.get("execution", {})
    lp = ex.get("lp", {})
    if not lp.get("enabled", False):
        logger.error("execution.lp.enabled is false — refusing to run. Set it true to start.")
        return
    store = Store(cfg["data_dir"])
    armed = bool(ex.get("armed", False))

    logger.info("LP selection: scanning reward markets (scan_limit=%s, min_pool=%s, "
                "max_min_size=%s)...", lp.get("scan_limit", 60), lp.get("min_pool", 20),
                lp.get("max_min_size", 100))
    cands = scan_candidates(scan_limit=lp.get("scan_limit", 60),
                            min_pool=lp.get("min_pool", 20.0),
                            max_min_size=lp.get("max_min_size", 100),
                            hurdle_mo_pct=lp.get("hurdle_mo_pct", 10.0))
    budget = lp.get("budget_usd", 500.0)
    quotable = [c for c in cands
                if _quotable(c, lp.get("blackout_hours", 48.0), lp.get("inv_book_frac", 0.5))]
    cand_by_cid = {c["condition_id"]: c for c in cands if c.get("condition_id")}

    # --- startup reconciliation: count capital already committed in held LP positions
    # so a relaunch never stacks a fresh budget on top (the cap holds across restarts).
    holdings = _fetch_lp_holdings(cfg["poly_funder"], _lp_condition_ids(store))
    committed = sum(h["q_yes"] * h["yes_avg"] + h["q_no"] * h["no_avg"]
                    for h in holdings.values())
    available = max(0.0, budget - committed)
    if holdings:
        logger.info("LP reconcile: %d held position(s), $%.2f committed -> $%.2f of $%.0f free",
                    len(holdings), committed, available, budget)

    # NEW markets: quotable, not already held, funded from the REMAINING budget
    held_cids = set(holdings)
    new_quotable = [c for c in quotable if c["condition_id"] not in held_cids]
    logger.info("LP: %d scanned, %d quotable, %d new (held excluded)",
                len(cands), len(quotable), len(new_quotable))
    chosen, spent = allocate(new_quotable, available)
    markets = [m for m in (_to_market_state(c) for c in chosen) if m]

    # HELD markets: always include + seed inventory so they are MANAGED and COUNTED
    for cid, h in holdings.items():
        m = _to_market_state(cand_by_cid[cid]) if cid in cand_by_cid \
            else _held_only_market_state(cid, h)
        if not m:
            logger.warning("LP reconcile: held %s has no usable tokens — skipped", cid[:12])
            continue
        m.q_yes, m.q_no = h["q_yes"], h["q_no"]
        m.cash = -(h["q_yes"] * h["yes_avg"] + h["q_no"] * h["no_avg"])
        markets.append(m)

    if not markets:
        logger.error("no markets to quote or manage — nothing to do. Exiting.")
        return
    logger.info("LP active on %d market(s): $%.0f new budget-fit + $%.2f held. armed=%s",
                len(markets), spent, committed, armed)
    for m in markets:
        logger.info("  %-40s pool $%.0f/d min %d mid %.3f inv %+.0f", m.question[:40],
                    m.pool_day, int(m.min_size), m.mid, m.inv)

    pm = PolymarketExecutor(store, private_key=cfg["poly_private_key"],
                            funder=cfg["poly_funder"], armed=armed,
                            signature_type=cfg["poly_signature_type"])

    lpx = LpExecutor(
        markets=markets, pm_exec=pm, store=store, armed=armed,
        refresh_s=lp.get("refresh_s", 20.0), poll_s=lp.get("poll_s", 5.0),
        book_poll_s=lp.get("book_poll_s", 10.0),
        reward_poll_s=lp.get("reward_poll_s", 900.0), metrics_s=lp.get("metrics_s", 60.0),
        reprice_eps=lp.get("reprice_eps", 0.005),
        half_spread_ticks=lp.get("half_spread_ticks", 1),
        max_open_quotes=lp.get("max_open_quotes", 20),
        budget_usd=lp.get("budget_usd", 500.0), size_mult=lp.get("size_mult", 1.0),
        inv_cap_mult=lp.get("inv_cap_mult", 3.0), inv_book_frac=lp.get("inv_book_frac", 0.5),
        jump_halt_pct=lp.get("jump_halt_pct", 0.15),
        kill_drawdown_frac=lp.get("kill_drawdown_frac", 0.15),
        blackout_hours=lp.get("blackout_hours", 48.0),
        min_pm_notional=ex.get("min_pm_notional", 1.0),
        maker_address=cfg["poly_funder"], signature_type=cfg["poly_signature_type"])

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lpx.shutdown)
        except NotImplementedError:
            pass

    try:
        await asyncio.gather(
            lpx.book_refresh_loop(), lpx.quoting_loop(), lpx.fill_poll_loop(),
            lpx.reward_poll_loop(), lpx.metrics_loop())
    finally:
        logger.warning("LP shutting down — cancelling all resting quotes")
        await lpx.cancel_all()


if __name__ == "__main__":
    asyncio.run(main())
