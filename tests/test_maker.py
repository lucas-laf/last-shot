"""MakerExecutor unit tests: no network, no credentials."""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone

from src.execution.betfair_executor import BfFill
from src.execution.maker_executor import MakerExecutor, MakerQuote
from src.execution.polymarket_executor import FillResult
from src.models import Quote
from src.signals import fees
from src.signals.engine import PairState
from src.storage import Store


# ---------- fakes ----------

class FakePM:
    def __init__(self, fills=None):
        self.armed = True
        self.placed = []
        self.cancelled = []
        self._fills = fills or {}      # order_id -> get_order dict
        self._n = 0

    def build_order(self, token, side, price, size, neg_risk=None, tick_size=None):
        return {"token_id": token, "side": side, "price": price, "size": size}

    async def place(self, order, order_type="FOK"):
        self._n += 1
        oid = f"oid-{self._n}"
        self.placed.append((order, order_type, oid))
        if order_type == "FOK":        # unwind reverse — report a full fill
            return {"order": order, "status": "matched", "order_id": oid,
                    "resp": {"size_matched": order["size"]}}
        return {"order": order, "status": "live", "order_id": oid}

    async def cancel(self, order_id):
        self.cancelled.append(order_id)
        return {"cancelled": True, "order_id": order_id}

    async def get_order(self, order_id):
        return self._fills.get(order_id, {})

    async def confirm_fill(self, ack):
        sz = ack.get("resp", {}).get("size_matched", ack.get("order", {}).get("size", 0))
        return FillResult(size=sz, avg_price=ack.get("order", {}).get("price", 0),
                          source="test", raw={})


class FakeBF:
    def __init__(self, killed=False, min_stake=2.0):
        self.armed = True
        self.killed = killed
        self.min_stake = min_stake
        self.orders = []

    def build_order(self, m, sel, side, prob, size_gbp):
        o = {"market": m, "sel": sel, "side": side, "prob": prob, "size_gbp": size_gbp}
        self.orders.append(o)
        return o

    async def place(self, order):
        return {"order": order, "size_matched": 0.0 if self.killed else order["size_gbp"],
                "order_status": "x", "rtt_ms": 5.0}

    def parse_fill(self, ack):
        return BfFill(matched_stake=ack["size_matched"], avg_odds=2.0, status="x")


def make_state(bf_bid=0.50, bf_ask=0.52, pm_bid=0.40, pm_ask=0.60, no_token="notok"):
    now = datetime.now(timezone.utc)
    return PairState(
        betfair_market_id="1.1", betfair_selection_id="42",
        polymarket_market_id="0xabc", polymarket_token_id="yes",
        polymarket_no_token_id=no_token, outcome_name="Test",
        bf=Quote(bid=bf_bid, ask=bf_ask, bid_size=100, ask_size=100),
        pm=Quote(bid=pm_bid, ask=pm_ask, bid_size=100, ask_size=100),
        bf_ts=now, pm_ts=now)


def make_maker(tmp_path, pm, bf, states=None, **kw):
    store = Store(str(tmp_path))
    states = states if states is not None else [make_state()]
    ex = MakerExecutor(states=states, bf_exec=bf, pm_exec=pm, store=store, rtt=None,
                       categories={"1.1": "tennis"}, armed=True, commission=0.05,
                       margin=0.01, **kw)
    return ex, store


def _quote(s, ex, side="bid"):
    t = ex._compute_target(s, side)
    return MakerQuote(
        pair_key=("1.1", "42"), state=s, side=t.side, exec_token=t.exec_token,
        exec_price=t.exec_price, pm_is_short=t.pm_is_short, bf_ref=t.bf_ref,
        bf_hedge_side=t.bf_hedge_side, locked=t.locked, size=ex.quote_shares,
        order_id="oid-1", placed_ts=time.perf_counter(),
        reserved_usd=ex.quote_shares * t.exec_price)


# ---------- _compute_target (pure) ----------

def test_compute_target_bid(tmp_path):
    ex, _ = make_maker(tmp_path, FakePM(), FakeBF())
    t = ex._compute_target(make_state(), "bid")
    # betfair_sell(0.50, .05)=0.475; floor((0.475-0.01)*100)/100 = 0.46
    assert t.exec_token == "yes" and not t.pm_is_short and t.bf_hedge_side == "sell"
    assert t.exec_price == 0.46
    assert abs(t.locked - (0.475 - 0.46)) < 1e-9


def test_compute_target_ask_via_no_token(tmp_path):
    ex, _ = make_maker(tmp_path, FakePM(), FakeBF())
    t = ex._compute_target(make_state(), "ask")
    bb = fees.betfair_buy(0.52, 0.05)
    q = math.ceil((bb + 0.01) * 100) / 100
    assert t.pm_is_short and t.exec_token == "notok" and t.bf_hedge_side == "buy"
    assert t.exec_price == round(1.0 - q, 2)     # buy NO at 1 - q_sell
    assert abs(t.locked - (q - bb)) < 1e-9


def test_compute_target_ask_skipped_without_no_token(tmp_path):
    ex, _ = make_maker(tmp_path, FakePM(), FakeBF())
    assert ex._compute_target(make_state(no_token=""), "ask") is None


def test_compute_target_gate_outside_spread(tmp_path):
    ex, _ = make_maker(tmp_path, FakePM(), FakeBF())
    # pm bid 0.50 > q_buy 0.46 -> not inside spread -> None
    assert ex._compute_target(make_state(pm_bid=0.50), "bid") is None


# ---------- _reconcile ----------

def test_reconcile_places_and_reserves(tmp_path):
    pm, bf = FakePM(), FakeBF()
    s = make_state()
    ex, _ = make_maker(tmp_path, pm, bf, states=[s])
    asyncio.run(ex._reconcile(s, "bid"))
    key = "bid:1.1:42"
    assert key in ex._quotes
    assert any(ot == "GTC" for _, ot, _ in pm.placed)
    assert abs(ex._reserved_usd - ex._quotes[key].reserved_usd) < 1e-9


def test_reconcile_cancels_when_ineligible(tmp_path):
    pm, bf = FakePM(), FakeBF()
    s = make_state()
    ex, _ = make_maker(tmp_path, pm, bf, states=[s])
    asyncio.run(ex._reconcile(s, "bid"))
    # make the Betfair quote stale -> ineligible -> existing quote cancelled
    s.bf_ts = datetime(2020, 1, 1, tzinfo=timezone.utc)
    asyncio.run(ex._reconcile(s, "bid"))
    assert "bid:1.1:42" not in ex._quotes
    assert pm.cancelled and abs(ex._reserved_usd) < 1e-9


def test_reconcile_capital_cap_blocks_place(tmp_path):
    pm, bf = FakePM(), FakeBF()
    s = make_state()
    ex, _ = make_maker(tmp_path, pm, bf, states=[s], float_usd=0.5)  # < one quote's reserve
    asyncio.run(ex._reconcile(s, "bid"))
    assert ex._quotes == {} and abs(ex._reserved_usd) < 1e-9


# ---------- fill -> hedge ----------

def test_fill_hedge_locks(tmp_path):
    pm, bf = FakePM(), FakeBF(killed=False)
    s = make_state()
    ex, store = make_maker(tmp_path, pm, bf, states=[s], one_shot=False)
    q = _quote(s, ex, "bid")
    ex._quotes["bid:1.1:42"] = q
    ex._reserved_usd = q.reserved_usd
    asyncio.run(ex._handle_fill("bid:1.1:42", q, matched=5.0, info={}))
    row = store._conn.execute(
        "select pair_status, is_maker, bf_filled_stake from live_trades order by id desc limit 1"
    ).fetchone()
    assert row[0] == "locked" and row[1] == 1
    assert bf.orders and bf.orders[0]["size_gbp"] == round(5.0 * 0.50, 2)  # hedge sized to fill @ bf.bid
    assert ex._quotes == {} and abs(ex._reserved_usd) < 1e-9


def test_fill_hedge_unwind_on_bf_kill(tmp_path):
    pm, bf = FakePM(), FakeBF(killed=True)
    s = make_state()
    ex, store = make_maker(tmp_path, pm, bf, states=[s], one_shot=False)
    q = _quote(s, ex, "bid")
    ex._quotes["bid:1.1:42"] = q
    ex._reserved_usd = q.reserved_usd
    asyncio.run(ex._handle_fill("bid:1.1:42", q, matched=5.0, info={}))
    row = store._conn.execute(
        "select pair_status from live_trades order by id desc limit 1").fetchone()
    assert row[0] == "unwound"
    # reverse FOK sell placed to flatten
    assert any(o["side"] == "sell" and ot == "FOK" for o, ot, _ in pm.placed)


def test_one_shot_cancels_all_and_disarms(tmp_path):
    pm, bf = FakePM(), FakeBF(killed=False)
    s = make_state()
    ex, _ = make_maker(tmp_path, pm, bf, states=[s], one_shot=True, max_live_arbs=1)
    # a second resting quote that one-shot disarm must cancel
    other = _quote(s, ex, "ask")
    other.order_id = "oid-other"
    ex._quotes["ask:1.1:42"] = other
    ex._reserved_usd += other.reserved_usd
    q = _quote(s, ex, "bid")
    ex._quotes["bid:1.1:42"] = q
    ex._reserved_usd += q.reserved_usd
    asyncio.run(ex._handle_fill("bid:1.1:42", q, matched=5.0, info={}))
    assert ex.armed is False
    assert ex._quotes == {} and abs(ex._reserved_usd) < 1e-9
    assert "oid-other" in pm.cancelled       # the other quote was cancelled on disarm


def test_fill_race_noop_if_cancelled(tmp_path):
    pm, bf = FakePM(), FakeBF()
    s = make_state()
    ex, store = make_maker(tmp_path, pm, bf, states=[s])
    q = _quote(s, ex, "bid")
    # quote NOT in _quotes (quoting loop cancelled it just before) -> handle is a no-op
    asyncio.run(ex._handle_fill("bid:1.1:42", q, matched=5.0, info={}))
    n = store._conn.execute("select count(*) from live_trades").fetchone()[0]
    assert n == 0 and not bf.orders
