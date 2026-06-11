import pytest

from src.signals.fees import betfair_buy, betfair_sell, polymarket_buy, polymarket_sell


def test_zero_fees_are_identity():
    assert betfair_buy(0.5, 0.0) == pytest.approx(0.5)
    assert betfair_sell(0.5, 0.0) == 0.5
    assert polymarket_buy(0.5, 0.0) == 0.5
    assert polymarket_sell(0.5, 0.0) == 0.5


def test_betfair_buy_commission_raises_cost():
    # back at 2.0 with 5% commission: win 1*(1-0.05)=0.95 per 1 staked
    # effective prob = 1/(1+0.95) = 0.5128...
    assert betfair_buy(0.5, 0.05) == pytest.approx(1 / 1.95)
    assert betfair_buy(0.5, 0.05) > 0.5


def test_betfair_sell_commission_lowers_proceeds():
    assert betfair_sell(0.5, 0.05) == pytest.approx(0.475)


def test_polymarket_fee_proportional_to_min_p():
    # 3% schedule: fee at p=0.9 uses min(0.9, 0.1)=0.1
    assert polymarket_buy(0.9, 0.03) == pytest.approx(0.9 + 0.03 * 0.1)
    assert polymarket_sell(0.1, 0.03) == pytest.approx(0.1 - 0.03 * 0.1)
