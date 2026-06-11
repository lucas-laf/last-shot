from src.models import Quote
from src.tracking.normalize import quote_from_betfair, quote_from_polymarket_book


def test_betfair_back_lay_to_prob_band():
    # back 2.0 (buy prob 0.5), lay 2.02 (sell prob ~0.495)
    q = quote_from_betfair(best_back=(2.0, 100.0), best_lay=(2.02, 50.0))
    assert q.ask == 0.5
    assert abs(q.bid - 1 / 2.02) < 1e-9
    assert q.bid < q.ask
    assert q.ask_size == 200.0  # stake * odds = payout-equivalent shares


def test_betfair_empty_book_is_unpriced():
    q = quote_from_betfair(None, None)
    assert q.bid == 0.0 and q.ask == 1.0


def test_polymarket_top_of_book():
    q = quote_from_polymarket_book(
        bids={0.48: 100, 0.47: 500}, asks={0.52: 80, 0.55: 300}
    )
    assert q == Quote(bid=0.48, ask=0.52, bid_size=100, ask_size=80)


def test_polymarket_empty_book():
    q = quote_from_polymarket_book({}, {})
    assert q.bid == 0.0 and q.ask == 1.0
