from datetime import datetime, timedelta, timezone

from src.models import Platform, Quote, SignalType
from src.signals.engine import PairState, SignalEngine


def fresh_state(**kw) -> PairState:
    now = datetime.now(timezone.utc)
    s = PairState(
        betfair_market_id="1.1", betfair_selection_id="100",
        polymarket_market_id="0xabc", polymarket_token_id="t1",
        outcome_name="Arsenal", pm_taker_rate=0.03,
        bf_liquidity=100_000, pm_liquidity=10_000,
        bf_ts=now, pm_ts=now,
    )
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def engine(**kw) -> SignalEngine:
    return SignalEngine(commission=0.05, min_edge=0.02, **kw)


def test_no_signal_when_prices_agree():
    s = fresh_state(
        bf=Quote(bid=0.49, ask=0.51, bid_size=100, ask_size=100),
        pm=Quote(bid=0.49, ask=0.51, bid_size=100, ask_size=100),
    )
    assert engine().evaluate(s) == []


def test_lock_arb_fires_both_legs():
    # polymarket much cheaper to buy than betfair pays to sell
    s = fresh_state(
        bf=Quote(bid=0.60, ask=0.62, bid_size=100, ask_size=100),
        pm=Quote(bid=0.48, ask=0.50, bid_size=100, ask_size=100),
    )
    sigs = engine().evaluate(s)
    lock = [x for x in sigs if x.signal_type == SignalType.LOCK_ARB]
    assert len(lock) == 2
    platforms = {(x.bet_platform, x.side) for x in lock}
    assert (Platform.POLYMARKET, "buy") in platforms
    assert (Platform.BETFAIR, "sell") in platforms


def test_convergence_uses_deeper_book_as_reference():
    # betfair deeper, mid 0.61; polymarket buyable at 0.50 net < ref - edge
    s = fresh_state(
        bf=Quote(bid=0.60, ask=0.62, bid_size=100, ask_size=100),
        pm=Quote(bid=0.48, ask=0.50, bid_size=100, ask_size=100),
    )
    sigs = engine().evaluate(s)
    conv = [x for x in sigs if x.signal_type == SignalType.CONVERGENCE]
    assert len(conv) == 1
    assert conv[0].bet_platform == Platform.POLYMARKET
    assert conv[0].side == "buy"
    assert conv[0].reference_prob == 0.61


def test_cooldown_suppresses_repeat_fires():
    s = fresh_state(
        bf=Quote(bid=0.60, ask=0.62, bid_size=100, ask_size=100),
        pm=Quote(bid=0.48, ask=0.50, bid_size=100, ask_size=100),
    )
    e = engine(cooldown_seconds=120)
    assert e.evaluate(s)
    assert e.evaluate(s) == []


def test_stale_leg_suppresses_signals():
    s = fresh_state(
        bf=Quote(bid=0.60, ask=0.62, bid_size=100, ask_size=100),
        pm=Quote(bid=0.48, ask=0.50, bid_size=100, ask_size=100),
        pm_ts=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert engine().evaluate(s) == []


def test_edge_must_clear_fees_not_just_min_edge():
    # raw gap of 3c, but commission+taker fees eat it
    s = fresh_state(
        bf=Quote(bid=0.53, ask=0.55, bid_size=100, ask_size=100),
        pm=Quote(bid=0.48, ask=0.50, bid_size=100, ask_size=100),
    )
    sigs = engine().evaluate(s)
    assert all(x.signal_type != SignalType.LOCK_ARB for x in sigs)
