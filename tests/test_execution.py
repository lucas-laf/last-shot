"""Execution-module unit tests: no network, no credentials."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.execution.arb_executor import ArbExecutor, PendingShadow
from src.execution.betfair_executor import prob_to_marketable_odds, snap_odds
from src.execution.latency import RttMonitor
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
