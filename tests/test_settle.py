import pytest

from src.signals.settle import pnl
from src.signals.settle_live import _pm_leg


def test_pm_leg_naked_short_lost():
    # #53 Scotland shape: bought NO @0.41 (short), 'yes' won -> NO token lost.
    row = {"pm_filled_size": 5.0, "pm_filled_price": 0.41, "pm_is_short": 1}
    res, p, yes_won = _pm_leg(row, "yes")
    assert res == "lost" and p == pytest.approx(-2.05) and yes_won is True


def test_pm_leg_naked_long_won():
    row = {"pm_filled_size": 5.0, "pm_filled_price": 0.40, "pm_is_short": 0}
    res, p, _ = _pm_leg(row, "yes")
    assert res == "won" and p == pytest.approx(3.0)   # payout 5 - cost 2


def test_pm_leg_void_refund():
    row = {"pm_filled_size": 5.0, "pm_filled_price": 0.41, "pm_is_short": 0}
    res, p, _ = _pm_leg(row, "void")
    assert res == "void" and p == 0.0


def test_buy_win():
    assert pnl("buy", 0.4, 100, won=True) == pytest.approx(60.0)


def test_buy_lose():
    assert pnl("buy", 0.4, 100, won=False) == pytest.approx(-40.0)


def test_sell_win_means_outcome_happened():
    assert pnl("sell", 0.6, 100, won=True) == pytest.approx(-40.0)


def test_sell_lose_means_outcome_missed():
    assert pnl("sell", 0.6, 100, won=False) == pytest.approx(60.0)


def test_lock_arb_legs_net_positive():
    # buy PM at 0.50 net, sell BF at 0.57 net, 100 shares each
    for won in (True, False):
        total = pnl("buy", 0.50, 100, won) + pnl("sell", 0.57, 100, won)
        assert total == pytest.approx(7.0)
