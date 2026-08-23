"""ParlayAPI parsing.

The response shapes are not published anywhere reachable, so these cover the
layouts the parser is written to accept. Replace them with a captured fixture
once a real response is in hand.
"""

import pytest

from odds_watcher.parlayapi import ParlayApiClient, market_catalogue, parse_event, parse_quotes


def demo_payload():
    """A real /v1/try/baseball_mlb/odds response."""
    import json
    from pathlib import Path

    return json.loads(
        (Path(__file__).parent / "fixtures" / "parlay_api_demo.json").read_text(encoding="utf-8")
    )


def test_live_demo_payload_parses():
    """Captured from the API: events wrapped in a envelope, American prices."""
    quotes = parse_quotes(demo_payload())
    assert len(quotes) == 10
    assert {q.market for q in quotes} == {"h2h"}
    assert "bet365" in {q.bookmaker for q in quotes}


def test_live_american_prices_become_decimal():
    by_key = {(q.bookmaker, q.outcome): q.odds for q in parse_quotes(demo_payload())}
    assert by_key[("fanduel", "Arizona Diamondbacks")] == pytest.approx(1.7692, abs=1e-4)  # -130
    assert by_key[("fanduel", "Cincinnati Reds")] == pytest.approx(2.20, abs=1e-4)          # +120
    assert by_key[("kalshi", "Cincinnati Reds")] == pytest.approx(2.00, abs=1e-4)           # -100
    assert by_key[("draftkings", "Arizona Diamondbacks")] == pytest.approx(1.7752, abs=1e-4)  # -129


def test_live_event_parsing():
    events = [parse_event(raw) for raw in demo_payload()["events"]]
    assert events[0].name == "Arizona Diamondbacks vs Cincinnati Reds"
    assert events[0].sport_key == "baseball_mlb"
    assert events[0].id == "d197ab11d19211f51b349da57e14539b"
    assert all(e.start_ts for e in events)


def test_live_single_outcome_books_are_kept():
    """Kalshi and Polymarket quote one side only; that is still a tracked price."""
    quotes = parse_quotes(demo_payload())
    kalshi = [q for q in quotes if q.bookmaker == "kalshi"]
    assert len(kalshi) == 1
    assert kalshi[0].outcome == "Cincinnati Reds"


def test_live_market_timestamp_is_read():
    quotes = parse_quotes(demo_payload())
    fanduel = next(q for q in quotes if q.bookmaker == "fanduel")
    from odds_watcher.util import parse_timestamp

    assert fanduel.updated_ts == parse_timestamp("2026-08-23T17:47:31Z")


def test_odds_format_is_not_guessed():
    """A decimal 150.0 and an American +150 differ; the format decides."""
    from odds_watcher.parlayapi import _price

    assert _price(150, "american") == pytest.approx(2.5)
    assert _price(150, "decimal") == 150.0
    assert _price(-130, "american") == pytest.approx(1.7692, abs=1e-4)
    assert _price(1.91, "american") == 1.91  # already decimal, below the threshold


def test_parse_event_common_shape():
    event = parse_event({
        "id": "abc123", "sport_key": "baseball_mlb", "sport_title": "MLB",
        "commence_time": "2026-08-24T23:05:00Z",
        "home_team": "Phillies", "away_team": "Cardinals",
    })
    assert event.id == "abc123"
    assert event.name == "Phillies vs Cardinals"
    assert event.sport_key == "baseball_mlb"


def test_parse_event_nested_sport_and_alternate_names():
    event = parse_event({
        "event_id": 42, "start_time": 1787000000,
        "sport": {"key": "soccer_epl", "name": "EPL"},
        "home": {"name": "Arsenal"}, "away": {"name": "Chelsea"},
    })
    assert (event.id, event.sport_key) == ("42", "soccer_epl")
    assert event.home == "Arsenal"


def test_quotes_from_a_bookmaker_list():
    quotes = parse_quotes([{
        "id": "e1",
        "bookmakers": [{
            "key": "draftkings",
            "markets": [
                {"key": "h2h", "outcomes": [{"name": "Phillies", "price": 1.65},
                                            {"name": "Cardinals", "price": 2.30}]},
                {"key": "totals", "point": 8.5,
                 "outcomes": [{"name": "Over", "price": 1.91},
                              {"name": "Under", "price": 1.91}]},
            ],
        }],
    }])
    by_key = {(q.market, q.outcome): q for q in quotes}
    assert by_key[("h2h", "Phillies")].odds == 1.65
    assert by_key[("totals", "Over")].line == "8.5"
    assert all(q.bookmaker == "draftkings" for q in quotes)


def test_quotes_from_a_bookmaker_keyed_object():
    quotes = parse_quotes({"data": [{
        "event_id": "e2",
        "books": {"fanduel": {"spread": {"outcomes": [
            {"name": "Home", "price": 1.95, "point": -1.5}]}}},
    }]})
    assert len(quotes) == 1
    assert quotes[0].key == ("e2", "fanduel", "spread", "-1.5", "Home")


def test_player_props_combine_player_and_side():
    quotes = parse_quotes([{
        "id": "e3",
        "bookmakers": [{"key": "dk", "markets": [
            {"key": "batter_home_runs", "outcomes": [
                {"description": "Bryce Harper", "name": "Over", "price": 3.6, "point": 0.5},
                {"player": "Kyle Schwarber", "name": "Over", "price": 3.1, "point": 0.5},
            ]}]}],
    }])
    outcomes = {q.outcome for q in quotes}
    assert "Bryce Harper Over" in outcomes
    assert "Kyle Schwarber Over" in outcomes
    assert len({q.key for q in quotes}) == 2


def test_american_prices_are_converted_to_decimal():
    quotes = parse_quotes([{
        "id": "e4",
        "bookmakers": [{"key": "dk", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Fav", "price": -150},
            {"name": "Dog", "price": 130},
        ]}]}],
    }])
    prices = {q.outcome: q.odds for q in quotes}
    assert prices["Fav"] == pytest.approx(1.6667, abs=1e-3)
    assert prices["Dog"] == pytest.approx(2.30, abs=1e-3)


def test_unusable_rows_are_skipped():
    assert parse_quotes([{"id": "e5", "bookmakers": [{"key": "dk", "markets": [
        {"key": "h2h", "outcomes": [
            {"name": "A"},                      # no price
            {"price": 2.0},                     # no name
            {"name": "C", "price": "n/a"},      # unparseable
        ]}]}]}]) == []


def test_market_catalogue():
    payload = [{"id": "e6", "bookmakers": [{"key": "dk", "markets": [
        {"key": "h2h", "outcomes": [{"name": "A", "price": 2.0}, {"name": "B", "price": 1.9}]}]}]}]
    assert market_catalogue(payload) == {"h2h": {"dk": 2}}


def test_api_key_is_sent_as_a_header(monkeypatch):
    seen = {}

    def fake(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return [], {}

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers", fake)
    client = ParlayApiClient("secret", default_sport="baseball_mlb")
    client.get_events("baseball_mlb")

    assert seen["url"] == "https://parlay-api.com/v1/sports/baseball_mlb/events"
    assert seen["headers"]["X-API-Key"] == "secret"
    assert "secret" not in seen["url"]  # never in the query string


def test_endpoint_paths(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "odds_watcher.parlayapi.request_json_with_headers",
        lambda url, **kw: (calls.append(url), ([], {}))[1],
    )
    client = ParlayApiClient("k", default_sport="baseball_mlb", prop_markets=("all",))
    client.get_sports()
    client.get_odds_payloads(["e1"], [], sport="baseball_mlb")

    assert calls[0].endswith("/v1/sports")
    assert calls[1].endswith("/v1/sports/baseball_mlb/odds")
    assert calls[2].endswith("/v1/sports/baseball_mlb/props")
