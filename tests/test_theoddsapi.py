"""The Odds API v4: payload parsing, quota accounting, and endpoint routing."""

import json
from pathlib import Path

import pytest

from odds_watcher.theoddsapi import (
    TheOddsApiClient,
    UnsupportedByProvider,
    market_catalogue,
    parse_event,
    parse_quotes,
)
from odds_watcher.util import parse_timestamp


def payload():
    return json.loads(
        (Path(__file__).parent / "fixtures" / "the_odds_api_v4.json").read_text(encoding="utf-8")
    )


def test_parse_event():
    event = parse_event(payload()[0])
    assert event.id == "e91d0d0d2b1c9a1f"
    assert event.name == "Philadelphia Phillies vs St. Louis Cardinals"
    assert event.league_slug == "baseball_mlb"
    assert event.start_ts == parse_timestamp("2026-08-23T23:05:00Z")


def test_featured_markets_parse_with_bookmaker_keys():
    by_key = {(q.bookmaker, q.market, q.outcome): q for q in parse_quotes(payload())}
    assert by_key[("draftkings", "h2h", "Philadelphia Phillies")].odds == 1.65
    assert by_key[("betfair_ex_uk", "h2h", "St. Louis Cardinals")].odds == 2.34


def test_point_becomes_the_line():
    spreads = {q.outcome: q for q in parse_quotes(payload()) if q.market == "spreads"}
    assert spreads["Philadelphia Phillies"].line == "-1.5"
    assert spreads["St. Louis Cardinals"].line == "1.5"

    totals = {q.outcome: q for q in parse_quotes(payload()) if q.market == "totals"}
    assert totals["Over"].line == "8.5"
    assert totals["Over"].odds == 1.91


def test_player_props_name_the_player():
    """v4 puts the player in `description` and Over/Under in `name`."""
    props = {q.outcome: q for q in parse_quotes(payload()) if q.market == "batter_home_runs"}
    assert "Bryce Harper Over" in props
    assert props["Bryce Harper Over"].odds == 3.60
    assert props["Bryce Harper Over"].line == "0.5"
    assert "Kyle Schwarber Over" in props

    strikeouts = {q.outcome: q for q in parse_quotes(payload()) if q.market == "pitcher_strikeouts"}
    assert strikeouts["Zack Wheeler Over"].line == "6.5"


def test_players_do_not_collide_on_the_same_market_and_line():
    """Two players Over 0.5 home runs are different bets."""
    keys = [q.key for q in parse_quotes(payload()) if q.market == "batter_home_runs"]
    assert len(keys) == len(set(keys)) == 3


def test_market_catalogue_counts_props():
    catalogue = market_catalogue(payload())
    assert catalogue["h2h"] == {"draftkings": 2, "betfair_ex_uk": 2}
    assert catalogue["batter_home_runs"] == {"draftkings": 3}


def test_bad_prices_are_skipped():
    assert parse_quotes([{"id": "x", "bookmakers": [{"key": "dk", "markets": [
        {"key": "h2h", "outcomes": [
            {"name": "A", "price": None}, {"name": "B", "price": 0}, {"name": "", "price": 2.0}]}]}]}]) == []


def test_quota_cost_is_markets_times_regions():
    client = TheOddsApiClient("k", regions=("us", "uk"), featured_markets=("h2h", "spreads", "totals"))
    assert client.quota_cost(("h2h", "spreads", "totals")) == 6
    assert client.quota_cost(("h2h",)) == 2

    single = TheOddsApiClient("k", regions=("us",))
    assert single.quota_cost(("h2h", "spreads", "totals")) == 3


def test_quota_headers_are_recorded(monkeypatch):
    headers = {"x-requests-remaining": "19750", "x-requests-used": "250", "x-requests-last": "3"}
    monkeypatch.setattr(
        "odds_watcher.theoddsapi.request_json_with_headers",
        lambda url, **kw: ([], headers),
    )
    client = TheOddsApiClient("k", default_sport="baseball_mlb")
    client._featured_odds("baseball_mlb")
    assert (client.credits_remaining, client.credits_used, client.last_call_cost) == (19750, 250, 3)


def test_endpoints_and_api_key_parameter(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "odds_watcher.theoddsapi.request_json_with_headers",
        lambda url, **kw: (calls.append(url), ([], {}))[1],
    )
    client = TheOddsApiClient("secret", default_sport="baseball_mlb", regions=("us",))
    client.get_events("baseball_mlb")
    client._featured_odds("baseball_mlb")
    client._event_odds("baseball_mlb", "abc", ("batter_home_runs",))

    assert calls[0] == "https://api.the-odds-api.com/v4/sports/baseball_mlb/events?api_key=secret"
    assert "/v4/sports/baseball_mlb/odds?api_key=secret&regions=us&markets=h2h%2Cspreads%2Ctotals" in calls[1]
    assert "/v4/sports/baseball_mlb/events/abc/odds?" in calls[2]
    assert "markets=batter_home_runs" in calls[2]


def test_events_and_sports_do_not_consume_the_local_budget(monkeypatch):
    """Those endpoints are free upstream; spending local budget on them is wrong."""
    class Budget:
        def __init__(self):
            self.consumed = 0
            self.calls = 0

        def try_consume(self, cost=1):
            self.calls += 1
            self.consumed += cost
            return True

    monkeypatch.setattr(
        "odds_watcher.theoddsapi.request_json_with_headers", lambda url, **kw: ([], {})
    )
    budget = Budget()
    client = TheOddsApiClient("k", budget=budget, default_sport="baseball_mlb")
    client.get_sports()
    client.get_events("baseball_mlb")
    assert budget.consumed == 0

    # A featured poll costs markets x regions credits, not one request.
    client._featured_odds("baseball_mlb")
    assert budget.calls == 1
    assert budget.consumed == 3


def test_leagues_are_not_a_concept_here():
    with pytest.raises(UnsupportedByProvider):
        TheOddsApiClient("k").get_leagues("baseball_mlb")


def test_prop_calls_are_charged_per_market(monkeypatch):
    """Three prop markets across two regions is six credits, not one request."""
    monkeypatch.setattr(
        "odds_watcher.theoddsapi.request_json_with_headers", lambda url, **kw: ({}, {})
    )

    class Budget:
        def __init__(self):
            self.consumed = 0

        def try_consume(self, cost=1):
            self.consumed += cost
            return True

    budget = Budget()
    client = TheOddsApiClient("k", budget=budget, regions=("us", "uk"), default_sport="baseball_mlb")
    client._event_odds("baseball_mlb", "abc", ("batter_home_runs", "pitcher_strikeouts", "batter_hits"))
    assert budget.consumed == 6


def test_budget_refuses_a_call_it_cannot_cover(monkeypatch, tmp_path):
    from odds_watcher.odds_api import BudgetExceeded
    from odds_watcher.store import RequestBudget, Store

    monkeypatch.setattr(
        "odds_watcher.theoddsapi.request_json_with_headers", lambda url, **kw: ([], {})
    )
    store = Store(tmp_path / "budget.db")
    budget = RequestBudget(store, per_hour=4, per_day=100, clock=lambda: 1000.0)
    client = TheOddsApiClient("k", budget=budget, regions=("us",), default_sport="baseball_mlb")

    client._featured_odds("baseball_mlb")  # 3 credits, fits
    assert budget.remaining()[0] == 1

    with pytest.raises(BudgetExceeded):
        client._featured_odds("baseball_mlb")  # another 3 would not fit
    store.close()


def test_bookmakers_are_sampled_from_the_configured_sport(monkeypatch):
    """Sampling an unrelated sport would list the wrong books."""
    seen = []

    def fake(url, **kw):
        seen.append(url)
        return payload(), {}

    monkeypatch.setattr("odds_watcher.theoddsapi.request_json_with_headers", fake)
    client = TheOddsApiClient("k", default_sport="baseball_mlb")
    rows = client.get_bookmakers()

    assert any("/sports/baseball_mlb/odds" in url for url in seen)
    assert not any("/sports/americanfootball" in url for url in seen)
    assert ("draftkings", "DraftKings") in rows
    assert ("betfair_ex_uk", "Betfair") in rows


def test_bookmakers_fall_back_to_upcoming_without_a_sport(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "odds_watcher.theoddsapi.request_json_with_headers",
        lambda url, **kw: (seen.append(url), ([], {}))[1],
    )
    TheOddsApiClient("k").get_bookmakers()
    assert any("/sports/upcoming/odds" in url for url in seen)
