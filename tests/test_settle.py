import pytest

from src.signals.settle import pnl


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
