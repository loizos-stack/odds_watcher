"""Payload parsing — the API has shipped more than one shape, so be tolerant."""

from odds_watcher.odds_api import OddsApiClient, parse_event, parse_quotes
from odds_watcher.util import parse_timestamp


def test_parse_event_documented_shape():
    event = parse_event(
        {
            "id": "12345",
            "sport": "football",
            "league": {"name": "Premier League", "slug": "eng-pl"},
            "startTime": "2026-08-22T19:00:00Z",
            "homeParticipant": {"id": 1, "name": "Arsenal"},
            "awayParticipant": {"id": 2, "name": "Chelsea"},
        }
    )
    assert event.id == "12345"
    assert event.name == "Arsenal vs Chelsea"
    assert event.league == "Premier League"
    assert event.start_ts == parse_timestamp("2026-08-22T19:00:00Z")


def test_parse_event_flat_shape():
    event = parse_event(
        {"eventId": 7, "date": 1787000000, "home": "Ajax", "away": "PSV", "league": "Eredivisie"}
    )
    assert (event.id, event.home, event.away, event.start_ts) == ("7", "Ajax", "PSV", 1787000000.0)


def test_parse_event_rejects_incomplete_rows():
    assert parse_event({"home": "A", "away": "B"}) is None
    assert parse_event("nonsense") is None


def test_parse_quotes_documented_shape():
    quotes = parse_quotes(
        [
            {
                "eventId": "1",
                "bookmakers": ["bet365", "betano"],
                "markets": [
                    {
                        "market": "moneyline",
                        "outcomes": [
                            {"name": "Home", "odds": 2.5, "bookmaker": "Bet365", "timestamp": 1700},
                            {"name": "Away", "odds": 2.9, "bookmaker": "betano", "timestamp": 1700},
                        ],
                    }
                ],
            }
        ]
    )
    assert [(q.bookmaker, q.outcome, q.odds) for q in quotes] == [
        ("bet365", "Home", 2.5),
        ("betano", "Away", 2.9),
    ]
    assert quotes[0].updated_ts == 1700.0


def test_parse_quotes_bookmaker_nested_shape_and_line():
    quotes = parse_quotes(
        {
            "data": [
                {
                    "id": "9",
                    "bookmakers": {
                        "bet365": {
                            "markets": [
                                {
                                    "market": "spreads",
                                    "marketLine": -1.5,
                                    "outcomes": [{"name": "Home", "odds": "1.90"}],
                                }
                            ]
                        }
                    },
                }
            ]
        }
    )
    assert len(quotes) == 1
    assert quotes[0].key == ("9", "bet365", "spreads", "-1.5", "Home")
    assert quotes[0].odds == 1.90


def test_parse_quotes_single_object_and_default_event_id():
    quotes = parse_quotes(
        {"markets": {"moneyline": {"Home": {"odds": 1.75, "bookmaker": "betano"}}}},
        default_event_id="abc",
    )
    assert quotes[0].event_id == "abc"
    assert quotes[0].odds == 1.75


def test_parse_quotes_skips_prices_without_a_bookmaker_or_value():
    payload = [{"eventId": "1", "markets": [{"market": "moneyline", "outcomes": [
        {"name": "Home", "odds": 2.0},          # no bookmaker
        {"name": "Away", "odds": None, "bookmaker": "bet365"},  # no price
        {"name": "Draw", "odds": 0, "bookmaker": "bet365"},     # nonsense price
    ]}]}]
    assert parse_quotes(payload) == []


def test_parse_quotes_deduplicates():
    payload = [
        {"eventId": "1", "markets": [{"market": "moneyline", "outcomes": [
            {"name": "Home", "odds": 2.0, "bookmaker": "bet365"}]}]},
        {"eventId": "1", "markets": [{"market": "moneyline", "outcomes": [
            {"name": "Home", "odds": 2.0, "bookmaker": "bet365"}]}]},
    ]
    assert len(parse_quotes(payload)) == 1


def test_parse_quotes_empty_payloads():
    assert parse_quotes(None) == []
    assert parse_quotes([]) == []
    assert parse_quotes({"data": []}) == []


def test_client_sends_api_key_and_paths(monkeypatch):
    calls = []

    def fake_request(url, **kwargs):
        calls.append((url, kwargs.get("method", "GET")))
        return []

    monkeypatch.setattr("odds_watcher.odds_api.request_json", fake_request)
    client = OddsApiClient("secret", base_url="https://api2.odds-api.io/v3")
    client.get_events("football", league="eng-pl")
    client.get_multi_odds(["1", "2"], ["bet365", "betano"])
    client.select_bookmakers(["bet365", "betano"])

    assert calls[0] == ("https://api2.odds-api.io/v3/events?apiKey=secret&sport=football&league=eng-pl", "GET")
    assert "odds/multi?apiKey=secret&eventIds=1%2C2&bookmakers=bet365%2Cbetano" in calls[1][0]
    assert calls[2][1] == "PUT"


def test_multi_odds_falls_back_to_single_event_requests(monkeypatch):
    from odds_watcher.http import HttpError

    seen = []

    def fake_request(url, **kwargs):
        seen.append(url)
        if "odds/multi" in url:
            raise HttpError(403, "not on your plan", url)
        return [{"eventId": "1", "markets": [{"market": "moneyline", "outcomes": [
            {"name": "Home", "odds": 2.0, "bookmaker": "bet365"}]}]}]

    monkeypatch.setattr("odds_watcher.odds_api.request_json", fake_request)
    client = OddsApiClient("secret")
    quotes = client.get_multi_odds(["1", "2"], ["bet365"])

    assert len(quotes) == 2
    assert client.supports_multi is False
    assert sum("odds/multi" in url for url in seen) == 1


def test_budget_stops_requests():
    from odds_watcher.odds_api import BudgetExceeded

    class Exhausted:
        def try_consume(self):
            return False

    client = OddsApiClient("secret", budget=Exhausted())
    try:
        client.get_events("football")
    except BudgetExceeded:
        return
    raise AssertionError("expected BudgetExceeded")
