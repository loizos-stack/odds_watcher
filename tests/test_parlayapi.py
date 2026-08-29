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
    # canonical_event_id, not `id`: the odds endpoint numbers the same fixture
    # differently under `id`, and joining on it discards every price.
    assert events[0].id == "4e1dce87d4d7034d"
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
    assert "/v1/sports/baseball_mlb/odds?" in calls[1]
    # Without an explicit markets list the API returns h2h only.
    assert "markets=h2h%2Cspreads%2Ctotals" in calls[1]
    assert "/v1/sports/baseball_mlb/props?" in calls[2]


def test_sports_listing_unwraps_its_envelope(monkeypatch):
    """Their odds are wrapped as {"events": [...]}; a listing is wrapped too."""
    shapes = [
        {"sports": [{"key": "baseball_mlb", "title": "MLB"}]},
        {"data": [{"key": "baseball_mlb", "title": "MLB"}]},
        [{"key": "baseball_mlb", "title": "MLB"}],
    ]
    for shape in shapes:
        monkeypatch.setattr(
            "odds_watcher.parlayapi.request_json_with_headers", lambda url, _s=shape, **kw: (_s, {})
        )
        assert ParlayApiClient("k").get_sports() == [("baseball_mlb", "MLB")], shape


def test_event_blocks_are_not_mistaken_for_listings():
    """Guard: unwrapping must never swallow an event's own bookmakers key."""
    quotes = parse_quotes({"events": [{
        "id": "e1",
        "bookmakers": [{"key": "bet365", "markets": [
            {"key": "h2h", "outcomes": [{"name": "A", "price": -130}]}]}],
    }]})
    assert len(quotes) == 1
    assert quotes[0].bookmaker == "bet365"


def test_allowance_is_read_from_headers_or_body(monkeypatch):
    """The body is authoritative; only quota headers are trusted, not throttles."""
    cases = [
        (({"credits_remaining": 858, "events": []}, {}), 858),
        (([], {"x-credits-remaining": "873"}), 873),
        (([], {"x-requests-remaining": "500"}), 500),
        (({"requests_remaining": 412, "events": []}, {}), 412),
        (({"demo_remaining_hour": 59, "events": []}, {}), 59),
    ]
    for response, expected in cases:
        monkeypatch.setattr(
            "odds_watcher.parlayapi.request_json_with_headers", lambda url, _r=response, **kw: _r
        )
        client = ParlayApiClient("k")
        client.fetch_quota()
        assert client.credits_remaining == expected, response


def test_allowance_stays_unknown_when_nothing_reports_it(monkeypatch):
    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers",
                        lambda url, **kw: ({"events": []}, {}))
    client = ParlayApiClient("k")
    assert client.fetch_quota()["remaining"] is None


def test_flat_prop_rows_are_parsed():
    """The props endpoint returns rows, not nested bookmakers/markets/outcomes."""
    quotes = parse_quotes({"props": [
        {"event_id": "e1", "bookmaker": "draftkings", "player_name": "Aaron Judge",
         "market": "player_home_runs", "line": 0.5,
         "over_price": 260, "under_price": -340},
        {"event_id": "e1", "bookmaker": "pinnacle", "player_name": "Zack Wheeler",
         "market": "player_strikeouts", "line": 6.5,
         "over_price": -115, "under_price": -105},
    ]})
    by_key = {(q.bookmaker, q.market, q.outcome): q for q in quotes}

    judge_over = by_key[("draftkings", "player_home_runs", "Aaron Judge Over")]
    assert judge_over.odds == pytest.approx(3.60, abs=1e-2)   # +260
    assert judge_over.line == "0.5"

    judge_under = by_key[("draftkings", "player_home_runs", "Aaron Judge Under")]
    assert judge_under.odds == pytest.approx(1.294, abs=1e-3)  # -340

    assert ("pinnacle", "player_strikeouts", "Zack Wheeler Over") in by_key


def test_flat_and_nested_payloads_both_parse():
    """A poll mixes an /odds response with a /props one."""
    nested = {"id": "e1", "bookmakers": [{"key": "bet365", "markets": [
        {"key": "h2h", "outcomes": [{"name": "A", "price": -130}]}]}]}
    flat = {"event_id": "e1", "bookmaker": "bet365", "player_name": "A Player",
            "market": "player_hits", "line": 1.5, "over_price": -120}

    quotes = parse_quotes([nested, flat])
    assert {q.market for q in quotes} == {"h2h", "player_hits"}


def test_flat_rows_without_the_essentials_are_skipped():
    assert parse_quotes([{"over_price": -110}]) == []                       # no event/book
    assert parse_quotes([{"event_id": "e", "bookmaker": "b", "over_price": -110}]) == []  # no market


def test_usage_endpoint_is_read(monkeypatch):
    monkeypatch.setattr(
        "odds_watcher.parlayapi.request_json_with_headers",
        lambda url, **kw: ({"usage": {"used": 480, "limit": 1000}}, {}),
    )
    quota = ParlayApiClient("k").fetch_quota()
    assert quota["used"] == 480
    assert quota["limit"] == 1000
    assert quota["remaining"] == 520


def test_prop_market_reference_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "odds_watcher.parlayapi.request_json_with_headers",
        lambda url, **kw: (calls.append(url),
                           ({"markets": [{"key": "player_home_runs", "title": "Home Runs"}]}, {}))[1],
    )
    rows = ParlayApiClient("k").prop_market_keys("baseball_mlb")
    assert rows == [("player_home_runs", "Home Runs")]
    assert calls[0].endswith("/v1/sports/baseball_mlb/props/markets")


def test_invalid_market_error_is_parsed():
    from odds_watcher.parlayapi import valid_markets_from_error

    message = (
        "HTTP 400 for https://parlay-api.com/v1/sports/baseball_mlb/odds?markets=all: "
        '{"detail":{"error":"INVALID_MARKET","message":"Invalid market \'all\'. '
        "Valid values are: alternate_spreads, alternate_totals, btts, h2h, spreads, "
        'totals, totals_1st_5_innings; any player_*/batter_*/pitcher_*/anytime"}}'
    )
    keys = valid_markets_from_error(message)
    assert "h2h" in keys and "totals" in keys and "totals_1st_5_innings" in keys
    assert "all" not in keys
    assert valid_markets_from_error("HTTP 500 upstream down") == ()


def test_all_markets_retries_with_the_set_the_api_accepts(monkeypatch):
    """"all" is not a market name; the API's own rejection names the valid set."""
    from odds_watcher.http import HttpError

    valid = "h2h, spreads, totals, team_totals, btts"
    calls = []

    def fake(url, **kw):
        calls.append(url)
        asked = url.split("markets=")[1].split("&")[0]
        if "all" in asked:
            raise HttpError(400, '{"detail":{"error":"INVALID_MARKET","message":'
                                 f'"Invalid market \'all\'. Valid values are: {valid}; any player_*"}}}}', url)
        if "btts" in asked:   # a soccer market this sport rejects
            raise HttpError(400, "INVALID_MARKET for this sport", url)
        return [], {}

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers", fake)
    client = ParlayApiClient("k", default_sport="baseball_mlb", featured_markets=("all",))
    client._sport_odds("baseball_mlb")

    # all -> the parsed set -> the core three
    assert len(calls) == 3
    assert "markets=h2h%2Cspreads%2Ctotals" in calls[2]
    # and the working set is remembered rather than rediscovered
    client._sport_odds("baseball_mlb")
    assert len(calls) == 4


def test_explicit_markets_are_narrowed_to_the_valid_ones(monkeypatch):
    from odds_watcher.http import HttpError

    calls = []

    def fake(url, **kw):
        calls.append(url)
        asked = url.split("markets=")[1].split("&")[0]
        if "moonlines" in asked:
            raise HttpError(400, '{"message":"Invalid market. Valid values are: h2h, totals;"}', url)
        return [], {}

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers", fake)
    client = ParlayApiClient("k", default_sport="baseball_mlb",
                             featured_markets=("h2h", "moonlines", "totals"))
    client._sport_odds("baseball_mlb")
    assert "markets=h2h%2Ctotals" in calls[1]


def test_usage_keeps_the_raw_reply(monkeypatch):
    """A bare "60 remaining" is ambiguous; the raw body disambiguates it."""
    body = {"plan": "free", "period": "month", "used": 940, "limit": 1000, "remaining": 60}
    monkeypatch.setattr(
        "odds_watcher.parlayapi.request_json_with_headers", lambda url, **kw: (body, {})
    )
    quota = ParlayApiClient("k").fetch_quota()
    assert quota["remaining"] == 60
    assert quota["raw"] == body
    assert quota["raw"]["period"] == "month"


def test_rate_limit_is_never_read_as_the_account_balance(monkeypatch):
    """rate_limit_per_sec is a throttle, not a balance; confusing them is severe."""
    body = {
        "credits_used": 142, "credits_remaining": 858, "credits_total": 1000,
        "tier": "free", "period_end": "2026-09-01T00:00:00+00:00",
        "plan": {"credits_per_month": 1000, "rate_limit_per_sec": 60},
    }
    monkeypatch.setattr(
        "odds_watcher.parlayapi.request_json_with_headers",
        lambda url, **kw: (body, {"x-ratelimit-remaining": "60"}),
    )
    quota = ParlayApiClient("k").fetch_quota()
    assert quota["remaining"] == 858     # not 60
    assert quota["used"] == 142
    assert quota["limit"] == 1000
    assert quota["resets"] == "2026-09-01T00:00:00+00:00"


def test_body_wins_over_a_rate_limit_header(monkeypatch):
    monkeypatch.setattr(
        "odds_watcher.parlayapi.request_json_with_headers",
        lambda url, **kw: ({"credits_remaining": 858}, {"x-ratelimit-remaining": "60"}),
    )
    client = ParlayApiClient("k")
    client.get_events("baseball_mlb")
    assert client.credits_remaining == 858


def test_the_valid_market_list_is_learned_once_for_all_sports(monkeypatch):
    """Probing per sport would waste a request on each of 89 of them."""
    from odds_watcher.http import HttpError

    probes = []

    def fake(url, **kw):
        asked = url.split("markets=")[1].split("&")[0]
        if "all" in asked:
            probes.append(url)
            raise HttpError(400, '{"message":"Invalid market. Valid values are: '
                                 'h2h, spreads, totals;"}', url)
        return [], {}

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers", fake)
    client = ParlayApiClient("k", featured_markets=("all",))
    for sport in ("baseball_mlb", "soccer_epl", "tennis_atp", "basketball_nba"):
        client._sport_odds(sport)

    assert len(probes) == 1          # one rejection teaches all four
    assert client._valid_markets == ("h2h", "spreads", "totals")


def test_learned_markets_survive_a_restart(monkeypatch, tmp_path):
    """Relearning through a rejection costs a request per sport, every run."""
    from odds_watcher.http import HttpError
    from odds_watcher.store import Store

    store = Store(tmp_path / "keys.db")
    probes = []

    def fake(url, **kw):
        asked = url.split("markets=")[1].split("&")[0]
        if "all" in asked:
            probes.append(url)
            raise HttpError(400, '{"message":"Invalid market. Valid values are: '
                                 'h2h, spreads, totals;"}', url)
        return [], {}

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers", fake)

    first = ParlayApiClient("k", featured_markets=("all",), market_cache=store)
    first._sport_odds("baseball_mlb")
    assert len(probes) == 1

    # A fresh client, as if the process had been restarted.
    second = ParlayApiClient("k", featured_markets=("all",), market_cache=store)
    second._sport_odds("soccer_epl")
    assert len(probes) == 1  # nothing was relearned
    assert second._game_markets("soccer_epl") == ("h2h", "spreads", "totals")
    store.close()


def test_a_broken_market_cache_never_breaks_a_poll(monkeypatch):
    """A cache is an optimisation; losing it must cost requests, not prices."""
    class Broken:
        def get_market_keys(self, sport, max_age, now=None):
            raise RuntimeError("disk gone")

        def save_market_keys(self, sport, keys, now=None):
            raise RuntimeError("disk gone")

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers",
                        lambda url, **kw: ([], {}))
    client = ParlayApiClient("k", featured_markets=("h2h",), market_cache=Broken())
    assert client._sport_odds("baseball_mlb") == []


def test_all_never_reaches_the_api_as_a_sport_key(monkeypatch):
    """SPORTS=all is the watcher's instruction to expand, not a key to send."""
    from odds_watcher.config import Config
    from odds_watcher.providers import build_client

    cfg = Config(odds_api_key="k", telegram_bot_token="t", telegram_chat_id="1",
                 odds_provider="parlay-api", sports=("all",), bookmakers=("Bet365",))
    client = build_client(cfg)
    assert client.default_sport == ""


def test_a_listing_samples_a_real_sport_when_none_is_configured(monkeypatch):
    """There is no cross-sport odds endpoint, so a listing must pick one."""
    asked = []

    def fake(url, **kw):
        asked.append(url)
        if "/v1/sports" in url and "/odds" not in url:
            return [{"key": "baseball_mlb", "title": "MLB"}], {}
        return [], {}

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers", fake)
    ParlayApiClient("k", featured_markets=("h2h",))._any_sport()

    assert not any("/all/odds" in url or "/upcoming/odds" in url for url in asked)


def test_bookmakers_come_from_the_reference_endpoint(monkeypatch):
    """Mining them from an odds payload fails on exactly the wrong key."""
    asked = []

    def fake(url, **kw):
        asked.append(url)
        return {"bookmakers": [{"key": "bet365", "title": "Bet365"},
                               {"key": "draftkings", "title": "DraftKings"}]}, {}

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers", fake)
    rows = ParlayApiClient("k", bookmakers=("Betano",)).get_bookmakers()

    assert rows == [("bet365", "Bet365"), ("draftkings", "DraftKings")]
    assert len(asked) == 1 and "/v1/bookmakers" in asked[0]
    # The listing must not be filtered by the very keys it exists to correct.
    assert "bookmakers=" not in asked[0]


def test_a_non_market_400_is_not_retried_with_fewer_markets(monkeypatch):
    """A rejected bookmaker key is not fixed by asking for less."""
    from odds_watcher.http import HttpError

    calls = []

    def fake(url, **kw):
        calls.append(url)
        if "markets=all" in url:
            raise HttpError(400, '{"message":"Invalid market. Valid values are: '
                                 'h2h, spreads, totals;"}', url)
        raise HttpError(400, '{"detail":{"error":"UNKNOWN_BOOKMAKER",'
                             '"message":"Unknown bookmaker key(s): [\'Betano\']."}}', url)

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers", fake)
    client = ParlayApiClient("k", featured_markets=("all",), bookmakers=("Betano",))
    with pytest.raises(HttpError, match="UNKNOWN_BOOKMAKER"):
        client._sport_odds("baseball_mlb")

    assert len(calls) == 2  # the probe and one retry, not a third on core markets


def test_props_are_fetched_only_for_the_named_sports(monkeypatch):
    """Props are a second request per sport: on 388 sports they set the bill."""
    asked = []

    def fake(url, **kw):
        asked.append(url)
        return [], {}

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers", fake)
    client = ParlayApiClient("k", featured_markets=("h2h",), prop_markets=("all",),
                             prop_sports=("baseball_mlb",))

    assert client.wants_props("baseball_mlb")
    assert not client.wants_props("table_tennis")

    client.get_odds_payloads([], (), sport="baseball_mlb")
    client.get_odds_payloads([], (), sport="table_tennis")

    props = [u for u in asked if "/props" in u]
    assert len(props) == 1 and "baseball_mlb" in props[0]
    # Game markets still cover every sport.
    assert len([u for u in asked if u.endswith("/odds") or "/odds?" in u]) == 2


def test_an_empty_prop_sports_still_means_every_sport(monkeypatch):
    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers",
                        lambda url, **kw: ([], {}))
    client = ParlayApiClient("k", prop_markets=("all",))
    assert client.wants_props("anything")


def test_events_and_odds_agree_on_the_fixture_id():
    """The two endpoints number the same fixture differently under `id`.

    Reading `id` on both sides produced two disjoint id sets: prices came back
    in their thousands and every one was discarded for belonging to an unknown
    fixture. The join has to be on canonical_event_id.
    """
    import json
    from pathlib import Path

    from odds_watcher.parlayapi import parse_event, parse_quotes

    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "parlay_api_demo.json").read_text()
    )
    blocks = payload["events"]

    event_ids = {parse_event(block).id for block in blocks}
    quote_ids = {quote.event_id for quote in parse_quotes(payload)}

    assert quote_ids
    assert quote_ids <= event_ids, "prices reference fixtures the event list does not have"
    # And specifically: the stable id, not the per-endpoint one.
    assert blocks[0]["canonical_event_id"] in event_ids
    assert blocks[0]["id"] not in event_ids


def test_a_payload_without_a_canonical_id_still_parses():
    """Not every provider (or endpoint) supplies one."""
    from odds_watcher.parlayapi import event_id_of

    assert event_id_of({"canonical_event_id": "stable", "id": "volatile"}) == "stable"
    assert event_id_of({"id": "volatile"}) == "volatile"
    assert event_id_of({"event_id": "e1"}) == "e1"
    assert event_id_of({}, default="fallback") == "fallback"


def test_the_id_filter_matches_on_the_canonical_id(monkeypatch):
    """`wanted` holds canonical ids, so the block filter must read the same field."""
    import json
    from pathlib import Path

    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "parlay_api_demo.json").read_text()
    )
    canonical = payload["events"][0]["canonical_event_id"]

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers",
                        lambda url, **kw: (payload, {}))
    client = ParlayApiClient("k", featured_markets=("h2h",))
    quotes = client.get_multi_odds([canonical], (), sport="baseball_mlb")

    assert quotes, "asking for a fixture by its canonical id returned nothing"
    assert {q.event_id for q in quotes} == {canonical}


def test_no_featured_markets_means_no_game_markets_request(monkeypatch):
    """Props only: the game-markets call is a request bought for nothing."""
    asked = []

    def fake(url, **kw):
        asked.append(url)
        return [{"id": "e1", "canonical_event_id": "c1", "bookmakers": []}], {}

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers", fake)
    client = ParlayApiClient("k", featured_markets=(), prop_markets=("all",))
    client.get_odds_payloads([], (), sport="baseball_mlb")

    assert len(asked) == 1
    assert "/props" in asked[0]
    assert not any(u.endswith("/odds") or "/odds?" in u for u in asked)


def test_a_sport_with_no_props_is_not_asked_again(monkeypatch, tmp_path):
    """Most sports have no props; each costs a request per poll to confirm."""
    from odds_watcher.store import Store

    store = Store(tmp_path / "noprops.db")
    asked = []

    def fake(url, **kw):
        asked.append(url)
        return [], {}

    monkeypatch.setattr("odds_watcher.parlayapi.request_json_with_headers", fake)
    client = ParlayApiClient("k", featured_markets=(), prop_markets=("all",),
                             market_cache=store)

    client.get_odds_payloads([], (), sport="table_tennis")
    assert len(asked) == 1
    client.get_odds_payloads([], (), sport="table_tennis")
    assert len(asked) == 1, "asked a second time for a sport known to have none"

    # And it survives a restart.
    fresh = ParlayApiClient("k", featured_markets=(), prop_markets=("all",),
                            market_cache=store)
    assert not fresh.wants_props("table_tennis")
    assert fresh.wants_props("baseball_mlb")
    store.close()


def test_a_props_error_also_marks_the_sport(monkeypatch, tmp_path):
    from odds_watcher.http import HttpError
    from odds_watcher.store import Store

    store = Store(tmp_path / "err.db")
    monkeypatch.setattr(
        "odds_watcher.parlayapi.request_json_with_headers",
        lambda url, **kw: (_ for _ in ()).throw(HttpError(404, "no props", url)),
    )
    client = ParlayApiClient("k", featured_markets=(), prop_markets=("all",),
                             market_cache=store)
    client.get_odds_payloads([], (), sport="darts")
    assert not client.wants_props("darts")
    store.close()
