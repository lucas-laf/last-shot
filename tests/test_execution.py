"""Execution-module unit tests: no network, no credentials."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.execution.arb_executor import ArbExecutor, ArbPlan, PendingShadow
from src.execution.betfair_executor import (BetfairExecutor, BfFill,
                                            prob_to_marketable_odds, snap_odds)
from src.execution.latency import RttMonitor
from src.execution.polymarket_executor import FillResult, PolymarketExecutor
from src.models import Platform, Quote, SignalType, Tick
from src.signals.engine import PairState, Signal
from src.storage import Store


# ---------- betfair odds ladder ----------

def test_snap_odds_back_rounds_down():
    assert snap_odds(2.013, "BACK") == 2.0
    assert snap_odds(3.07, "BACK") == 3.05
    assert snap_odds(7.3, "BACK") == 7.2


def test_snap_odds_lay_rounds_up():
    assert snap_odds(2.013, "LAY") == 2.02
    assert snap_odds(3.07, "LAY") == 3.1
    assert snap_odds(7.3, "LAY") == 7.4


def test_snap_odds_exact_ticks_unchanged():
    for side in ("BACK", "LAY"):
        assert snap_odds(1.5, side) == 1.5
        assert snap_odds(4.1, side) == 4.1


def test_prob_to_marketable_odds_bounds():
    assert prob_to_marketable_odds(0.99999, "BACK") == 1.01
    assert prob_to_marketable_odds(0.0001, "LAY") == 1000.0


# ---------- arb executor ----------

class _NullRtt(RttMonitor):
    def __init__(self):  # no store / client
        self._bf, self._pm = [], []

    def betfair_rtt_ms(self):
        return 10.0

    def polymarket_rtt_ms(self):
        return 100.0


def make_executor(tmp_path, **kw):
    store = Store(str(tmp_path))
    return ArbExecutor(
        store=store, bf_exec=None, pm_exec=None, rtt=_NullRtt(),
        categories={"1.1": "tennis"}, **kw), store


def make_signal(buy_platform=Platform.POLYMARKET, ask_size=20.0, bid_size=8.0):
    s = PairState(
        betfair_market_id="1.1", betfair_selection_id="42",
        polymarket_market_id="0xabc", polymarket_token_id="tok1",
        outcome_name="Test Player",
        bf=Quote(bid=0.50, ask=0.52, bid_size=bid_size, ask_size=30.0),
        pm=Quote(bid=0.55, ask=0.49, bid_size=12.0, ask_size=ask_size),
        bf_ts=datetime.now(timezone.utc), pm_ts=datetime.now(timezone.utc),
    )
    return Signal(signal_type=SignalType.LOCK_ARB, state=s,
                  bet_platform=buy_platform, side="buy",
                  entry_prob=0.49, reference_prob=0.50, edge_after_fees=0.01)


def test_arb_records_both_legs_min_sized(tmp_path):
    ex, store = make_executor(tmp_path)
    ex.on_signal(make_signal())
    rows = store._conn.execute(
        "select platform, side, size from shadow_orders order by platform").fetchall()
    assert len(rows) == 2
    # buy pm (ask_size 20), sell bf (bid_size 8) -> both legs sized to min(20, 8, 5)=5
    assert all(r[2] == 5.0 for r in rows)
    assert {(r[0], r[1]) for r in rows} == {("betfair", "sell"), ("polymarket", "buy")}


def test_arb_per_outcome_cap(tmp_path):
    ex, store = make_executor(tmp_path)
    ex.on_signal(make_signal())
    ex.on_signal(make_signal())  # same outcome -> capped
    n = store._conn.execute("select count(*) from shadow_orders").fetchone()[0]
    assert n == 2


def test_sell_leg_ignored(tmp_path):
    ex, store = make_executor(tmp_path)
    sig = make_signal()
    sig.side = "sell"
    ex.on_signal(sig)
    assert store._conn.execute("select count(*) from shadow_orders").fetchone()[0] == 0


def test_daily_capital_gate(tmp_path):
    ex, _ = make_executor(tmp_path, max_daily_capital=8.0)
    assert ex._capital_ok(5.0)
    assert not ex._capital_ok(5.0)  # 10 > 8
    assert ex._capital_ok(3.0)


# ---------- shadow resolution ----------

def tick(platform, market, outcome, bid, ask, bid_size=10.0, ask_size=10.0):
    return Tick(platform=platform, market_id=market, outcome_id=outcome,
                ts=datetime.now(timezone.utc),
                quote=Quote(bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size),
                source_mode="test")


def test_shadow_killed_early_then_captured(tmp_path):
    ex, store = make_executor(tmp_path)
    ex.on_signal(make_signal())
    import time as _t
    now = _t.time()
    # pm buy leg target: price 0.49. Worse ask before arrival -> killed_early
    key = ("polymarket", "0xabc", "tok1")
    ex._pending[key][0].arrival_ts = now + 10  # force "before arrival"
    ex.on_tick(tick(Platform.POLYMARKET, "0xabc", "tok1", bid=0.48, ask=0.55))
    row = store._conn.execute(
        "select captured, basis from shadow_orders where platform='polymarket'").fetchone()
    assert row == (0, "killed_early")
    # bf sell leg target: price 0.50 bid. Tick after arrival with bid intact -> captured
    key_bf = ("betfair", "1.1", "42")
    ex._pending[key_bf][0].arrival_ts = now - 1  # already arrived
    ex.on_tick(tick(Platform.BETFAIR, "1.1", "42", bid=0.50, ask=0.52))
    row = store._conn.execute(
        "select captured, basis from shadow_orders where platform='betfair'").fetchone()
    assert row == (1, "post_rtt_tick")


# ---------- Phase B: live execution state machine ----------

class FakePM:
    """Scripted Polymarket executor: confirm_fill pops from `fills`."""
    def __init__(self, fills):
        self.armed = True
        self.fills = list(fills)
        self.placed = []

    def build_order(self, token, side, price, size, neg_risk=None, tick_size=None):
        return {"token_id": token, "side": side, "price": price, "size": size}

    async def place(self, order, order_type="FOK"):
        self.placed.append((order, order_type))
        return {"order": order, "status": "matched", "order_id": "pmx", "sent": True}

    async def confirm_fill(self, ack):
        return self.fills.pop(0)


class FakeBF:
    def __init__(self, killed=False, min_stake=2.0):
        self.armed = True
        self.killed = killed
        self.min_stake = min_stake
        self.orders = []

    def build_order(self, market, sel, side, prob, size_gbp):
        o = {"market_id": market, "sel": sel, "side": side, "prob": prob, "size_gbp": size_gbp}
        self.orders.append(o)
        return o

    async def place(self, order):
        matched = 0.0 if self.killed else order["size_gbp"]
        return {"order": order, "size_matched": matched, "order_status": "x", "rtt_ms": 5.0}

    def parse_fill(self, ack):
        return BfFill(matched_stake=ack["size_matched"], avg_odds=2.0, status="x")


def _plan(shares=5.0, bf_price=0.8, pm_side="buy"):
    return ArbPlan(
        pm_token_id="tok1", pm_market_id="0xabc", pm_side=pm_side, pm_price=0.5,
        bf_market_id="1.1", bf_selection_id="42", bf_side="sell", bf_price=bf_price,
        shares=shares, category="soccer", outcome_name="Test", edge=0.02,
        decide_us=120.0, decision_ts="2026-06-13T00:00:00Z")


def _live_executor(tmp_path, pm, bf, **kw):
    store = Store(str(tmp_path))
    ex = ArbExecutor(store=store, bf_exec=bf, pm_exec=pm, rtt=_NullRtt(),
                     categories={"1.1": "soccer"}, armed=True, **kw)
    return ex, store


def _status(store):
    return store._conn.execute(
        "select pair_status from live_trades order by id desc limit 1").fetchone()[0]


def test_execute_live_locked(tmp_path):
    pm = FakePM([FillResult(size=5.0, avg_price=0.5, source="post_response", raw={})])
    bf = FakeBF(killed=False)
    ex, store = _live_executor(tmp_path, pm, bf)
    asyncio.run(ex._execute_live(_plan()))
    assert _status(store) == "locked"
    assert ex._live_count == 1
    assert not ex.armed  # one-shot auto-disarm
    # hedge sized to PM fill (5 * 0.8) and bf order was placed
    assert bf.orders and bf.orders[0]["size_gbp"] == 4.0


def test_execute_live_state_a_abort(tmp_path):
    pm = FakePM([FillResult(size=0.0, avg_price=0.5, source="get_order", raw={})])
    bf = FakeBF()
    ex, store = _live_executor(tmp_path, pm, bf)
    asyncio.run(ex._execute_live(_plan()))
    assert _status(store) == "neither"
    assert ex._live_count == 0           # killed PM does not consume the shot
    assert ex.armed                      # still armed
    assert bf.orders == []               # no hedge attempted


def test_hedge_sized_to_pm_fill_not_plan(tmp_path):
    # plan says 5 shares but PM only filled 3 -> hedge must size to 3, not 5
    pm = FakePM([FillResult(size=3.0, avg_price=0.5, source="post_response", raw={})])
    bf = FakeBF(killed=False)
    ex, store = _live_executor(tmp_path, pm, bf)
    asyncio.run(ex._execute_live(_plan(shares=5.0, bf_price=0.8)))
    assert bf.orders[0]["size_gbp"] == round(3.0 * 0.8, 2)   # not 5*0.8
    assert _status(store) == "locked"


def test_execute_live_unwind_on_bf_kill(tmp_path):
    # PM fills, Betfair killed -> unwind; flatten FOK then succeeds
    pm = FakePM([
        FillResult(size=5.0, avg_price=0.5, source="post_response", raw={}),  # open
        FillResult(size=5.0, avg_price=0.48, source="post_response", raw={}),  # flatten
    ])
    bf = FakeBF(killed=True)
    ex, store = _live_executor(tmp_path, pm, bf)
    asyncio.run(ex._execute_live(_plan()))
    assert _status(store) == "unwound"
    # bought at 0.50, sold-to-close at 0.48 -> cost (0.50-0.48)*5 = 0.10
    cost = store._conn.execute(
        "select unwind_cost from live_trades order by id desc limit 1").fetchone()[0]
    assert abs(cost - 0.10) < 1e-6


def test_unwind_fallback_to_gtc_alerts(tmp_path):
    # PM fills, Betfair killed, and the flatten FOK can't fill -> GTC + alert
    pm = FakePM([
        FillResult(size=5.0, avg_price=0.5, source="post_response", raw={}),  # open
        FillResult(size=0.0, avg_price=0.5, source="get_order", raw={}),       # flatten fails
    ])
    bf = FakeBF(killed=True)
    ex, store = _live_executor(tmp_path, pm, bf)
    asyncio.run(ex._execute_live(_plan()))
    assert _status(store) == "unwound"
    alerts = store._conn.execute(
        "select count(*) from exec_events where kind='unwind_alert'").fetchone()[0]
    assert alerts == 1
    # the reverse leg was placed twice: FOK flatten, then GTC fallback
    assert sum(1 for _, ot in pm.placed if ot == "GTC") == 1


def test_sub_min_hedge_triggers_unwind(tmp_path):
    # PM fills tiny so the hedge stake < Betfair min -> unwind, no bf order
    pm = FakePM([
        FillResult(size=1.0, avg_price=0.5, source="post_response", raw={}),
        FillResult(size=1.0, avg_price=0.5, source="post_response", raw={}),
    ])
    bf = FakeBF()
    ex, store = _live_executor(tmp_path, pm, bf, min_bf_stake_gbp=2.0)
    asyncio.run(ex._execute_live(_plan(shares=1.0, bf_price=0.5)))  # 1*0.5=0.5 < 2
    assert _status(store) == "unwound"
    assert bf.orders == []


# ---------- Phase B: one-shot gating + scheduling ----------

def test_should_fire_live_gate(tmp_path):
    ex, _ = _live_executor(tmp_path, FakePM([]), FakeBF(), max_live_arbs=1)
    assert ex._should_fire_live()
    ex._live_inflight = True
    assert not ex._should_fire_live()
    ex._live_inflight = False
    ex._live_count = 1
    assert not ex._should_fire_live()       # cap reached
    ex._live_count = 0
    ex.armed = False
    assert not ex._should_fire_live()        # disarmed


def test_one_shot_race_single_launch(tmp_path, monkeypatch):
    ex, store = _live_executor(tmp_path, FakePM([]), FakeBF(), max_arbs_per_outcome=99,
                               min_pm_notional=0.0, min_bf_stake_gbp=0.0)
    launched = []
    monkeypatch.setattr(ex, "_launch", lambda coro: (launched.append(coro), coro.close()))
    ex.on_signal(make_signal(buy_platform=Platform.POLYMARKET))
    ex.on_signal(make_signal(buy_platform=Platform.POLYMARKET))  # inflight -> blocked
    assert len(launched) == 1
    assert ex._live_inflight is True


def test_pm_sell_leg_not_fired_live(tmp_path, monkeypatch):
    # bet_platform=BETFAIR -> the PM leg is a SELL; we can't sell tokens we don't
    # hold, so it must stay shadow-only (no live launch).
    ex, store = _live_executor(tmp_path, FakePM([]), FakeBF(),
                               min_pm_notional=0.0, min_bf_stake_gbp=0.0)
    launched = []
    monkeypatch.setattr(ex, "_launch", lambda coro: (launched.append(coro), coro.close()))
    ex.on_signal(make_signal(buy_platform=Platform.BETFAIR))
    assert launched == []
    assert store._conn.execute("select count(*) from shadow_orders").fetchone()[0] == 2


def test_min_notional_skips_live_keeps_shadow(tmp_path, monkeypatch):
    # huge min notional -> live gated off, shadows still recorded
    ex, store = _live_executor(tmp_path, FakePM([]), FakeBF(), min_pm_notional=1000.0)
    launched = []
    monkeypatch.setattr(ex, "_launch", lambda coro: (launched.append(coro), coro.close()))
    ex.on_signal(make_signal())
    assert launched == []
    assert store._conn.execute("select count(*) from shadow_orders").fetchone()[0] == 2
    dec = store._conn.execute(
        "select payload_json from exec_events where kind='arb_decision'").fetchone()[0]
    assert '"min_notional_ok": false' in dec.lower() or '"min_notional_ok": False' in dec


# ---------- Phase B: dry-run via REAL executors (validates sim-flag plumbing) ----------

def _dry_run_executor(tmp_path, sim_killed=False, sim_fill=None):
    store = Store(str(tmp_path))
    pm = PolymarketExecutor(store, private_key="")          # no creds -> SHADOW
    pm._sim_fill_fraction = sim_fill
    bf = BetfairExecutor(client=None, store=store, armed=False)
    bf._sim_killed = sim_killed
    ex = ArbExecutor(store=store, bf_exec=bf, pm_exec=pm, rtt=_NullRtt(),
                     categories={"1.1": "soccer"}, armed=True)
    return ex, store


def test_dry_run_real_executors_locked(tmp_path):
    ex, store = _dry_run_executor(tmp_path, sim_killed=False, sim_fill=None)
    asyncio.run(ex._execute_live(_plan()))
    assert _status(store) == "locked"


def test_dry_run_real_executors_unwind(tmp_path):
    ex, store = _dry_run_executor(tmp_path, sim_killed=True, sim_fill=None)
    asyncio.run(ex._execute_live(_plan()))
    assert _status(store) == "unwound"


def test_dry_run_real_executors_abort(tmp_path):
    ex, store = _dry_run_executor(tmp_path, sim_killed=False, sim_fill=0.0)
    asyncio.run(ex._execute_live(_plan()))
    assert _status(store) == "neither"


# ---------- Phase B: paper-trader per-outcome cap ----------

def test_paper_trader_per_outcome_cap(tmp_path):
    from src.signals.paper_trader import PaperTrader
    store = Store(str(tmp_path))
    pt = PaperTrader(store, max_stake=100.0,
                     betfair_depth=lambda m, s: {}, polymarket_depth=lambda t: {},
                     max_trades_per_outcome=2)
    sig = make_signal()
    for _ in range(5):
        pt.on_signal(sig)
    n = store._conn.execute("select count(*) from paper_trades").fetchone()[0]
    assert n == 2
