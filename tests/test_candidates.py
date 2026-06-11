from datetime import datetime, timedelta, timezone

from src.matching.candidates import generate
from src.models import Market, Outcome, Platform


def bf_market(**kw) -> Market:
    d = dict(
        platform=Platform.BETFAIR, market_id="1.1", event_name="Arsenal v Chelsea",
        market_name="Match Odds", category="soccer", market_type="MATCH_ODDS",
        outcomes=[Outcome(outcome_id="100", name="Arsenal")],
        start_time=datetime(2026, 6, 20, 15, tzinfo=timezone.utc),
    )
    d.update(kw)
    return Market(**d)


def pm_market(**kw) -> Market:
    d = dict(
        platform=Platform.POLYMARKET, market_id="0x1",
        event_name="Arsenal vs. Chelsea", market_name="Will Arsenal win on 2026-06-20?",
        category="soccer", market_type="moneyline",
        outcomes=[Outcome(outcome_id="t1", name="Arsenal", no_token_id="t2")],
        start_time=datetime(2026, 6, 20, 15, tzinfo=timezone.utc),
    )
    d.update(kw)
    return Market(**d)


def test_matching_fixture_pairs():
    cands = generate([bf_market()], [pm_market()])
    assert len(cands) == 1
    assert cands[0].fuzz_score >= 60


def test_props_excluded_for_match_odds():
    prop = pm_market(market_name="Bukayo Saka: 1+ goals", market_type="props")
    assert generate([bf_market()], [prop]) == []


def test_moneyline_excluded_for_outrights():
    outright = bf_market(market_name="Winner 2026/27", market_type="OUTRIGHT",
                         event_name="English Premier League")
    pm = pm_market()  # a game moneyline
    assert generate([outright], [pm]) == []


def test_category_must_match():
    other = pm_market(category="politics", market_type="")
    assert generate([bf_market()], [other]) == []


def test_time_window():
    far = pm_market(start_time=datetime(2026, 6, 25, 15, tzinfo=timezone.utc))
    assert generate([bf_market()], [far], time_window_hours=12) == []
