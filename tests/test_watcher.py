"""End-to-end behaviour of one poll cycle, with the network faked out."""

from odds_watcher.http import HttpError
from odds_watcher.odds_api import Event, Quote
from odds_watcher.watcher import Watcher

KICKOFF = 1_700_000_000.0


def at(minutes_before):
    return KICKOFF - minutes_before * 60


class FakeApi:
    """Stands in for OddsApiClient."""

    def __init__(self, events, odds_by_call):
        self.events = events
        self.odds_by_call = list(odds_by_call)
        self.event_calls = 0
        self.odds_calls = []
        self.sports_asked = []
        self.budget = None

    def get_events(self, sport, league=None, limit=None):
        self.event_calls += 1
        return list(self.events)

    def get_multi_odds(self, event_ids, bookmakers, *, sport=""):
        self.odds_calls.append(list(event_ids))
        self.sports_asked.append(sport)
        return self.odds_by_call.pop(0) if self.odds_by_call else []


class FakeTelegram:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_message(self, text, **kwargs):
        if self.fail:
            raise HttpError(500, "boom", "https://api.telegram.org")
        self.sent.append(text)
        return {"ok": True}


EVENT = Event(id="e1", start_ts=KICKOFF, home="Ajax", away="PSV", sport="football", league="Eredivisie")
FAR_EVENT = Event(id="e2", start_ts=KICKOFF + 6 * 3600, home="A", away="B")


def quote(odds, event_id="e1", bookmaker="bet365"):
    return Quote(event_id, bookmaker, "moneyline", "", "Home", odds)


def make(config, store, odds_by_call, telegram=None, events=(EVENT,)):
    api = FakeApi(list(events), odds_by_call)
    telegram = telegram or FakeTelegram()
    return Watcher(config, api, telegram, store, clock=lambda: at(20)), api, telegram


def test_full_cycle_sends_one_alert(config, store):
    watcher, api, telegram = make(config, store, [[quote(2.00)], [quote(1.80)]])

    assert watcher.poll_once(at(20)) == []
    assert telegram.sent == []

    alerts = watcher.poll_once(at(5))
    assert len(alerts) == 1
    assert len(telegram.sent) == 1
    body = telegram.sent[0]
    assert "Ajax vs PSV" in body and "BET365" in body and "-10.0%" in body


def test_events_are_only_refetched_when_stale(config, store):
    watcher, api, _ = make(config, store, [[], [], []])
    watcher.poll_once(at(30))
    watcher.poll_once(at(29))
    assert api.event_calls == 1
    watcher.poll_once(at(29) + config.events_refresh_seconds + 1)
    assert api.event_calls == 2


def test_odds_are_not_requested_for_distant_events(config, store):
    watcher, api, _ = make(config, store, [[]], events=(FAR_EVENT,))
    assert watcher.poll_once(at(30)) == []
    assert api.odds_calls == []  # no request wasted on a fixture 6h out


def test_only_in_range_events_are_batched(config, store):
    watcher, api, _ = make(config, store, [[]], events=(EVENT, FAR_EVENT))
    watcher.poll_once(at(30))
    assert api.odds_calls == [["e1"]]


def test_failed_delivery_is_retried_on_the_next_poll(config, store):
    failing = FakeTelegram(fail=True)
    watcher, api, _ = make(config, store, [[quote(2.00)], [quote(1.80)], [quote(1.80)]], telegram=failing)
    watcher.poll_once(at(20))
    assert len(watcher.poll_once(at(5))) == 1
    assert failing.sent == []

    watcher.telegram = FakeTelegram()
    assert len(watcher.poll_once(at(4))) == 1  # still unsent, so still alertable
    assert len(watcher.telegram.sent) == 1


def test_delivered_alert_is_not_repeated(config, store):
    watcher, api, telegram = make(
        config, store, [[quote(2.00)], [quote(1.80)], [quote(1.80)]]
    )
    watcher.poll_once(at(20))
    watcher.poll_once(at(5))
    assert watcher.poll_once(at(4)) == []
    assert len(telegram.sent) == 1


def test_alerts_for_several_events_are_grouped(config, store):
    other = Event(id="e3", start_ts=KICKOFF, home="Roma", away="Lazio")
    watcher, api, telegram = make(
        config,
        store,
        [[quote(2.00), quote(2.00, event_id="e3")], [quote(1.70), quote(1.70, event_id="e3")]],
        events=(EVENT, other),
    )
    watcher.poll_once(at(20))
    alerts = watcher.poll_once(at(5))
    assert len(alerts) == 2
    assert len(telegram.sent) == 1  # one digest message
    assert "Ajax vs PSV" in telegram.sent[0] and "Roma vs Lazio" in telegram.sent[0]


def test_poll_interval_is_fast_near_kickoff_and_lazy_otherwise(config, store):
    watcher, api, _ = make(config, store, [[]], events=(EVENT,))
    watcher.refresh_events(at(30))
    assert watcher.seconds_until_next_poll(at(5)) == config.poll_interval_seconds

    watcher._events = [FAR_EVENT]
    assert watcher.seconds_until_next_poll(at(30)) == config.idle_poll_interval_seconds


def test_api_errors_do_not_kill_the_loop(config, store):
    class BrokenApi(FakeApi):
        def get_multi_odds(self, event_ids, bookmakers, *, sport=""):
            raise HttpError(503, "upstream down", "https://api2.odds-api.io/v3/odds/multi")

    watcher = Watcher(config, BrokenApi([EVENT], []), FakeTelegram(), store)
    assert watcher.poll_once(at(5)) == []


def test_network_failure_is_handled_like_any_other_outage(config, store):
    """A dropped connection must not escape as a traceback (cron `once` mode)."""
    from odds_watcher.http import TransportError

    class OfflineApi(FakeApi):
        def get_multi_odds(self, event_ids, bookmakers, *, sport=""):
            raise TransportError("api2.odds-api.io unreachable after 3 attempts")

    watcher = Watcher(config, OfflineApi([EVENT], []), FakeTelegram(), store)
    assert watcher.poll_once(at(5)) == []


def test_telegram_outage_keeps_the_alert_pending(config, store):
    from odds_watcher.http import TransportError

    class OfflineTelegram(FakeTelegram):
        def send_message(self, text, **kwargs):
            raise TransportError("api.telegram.org unreachable")

    watcher, api, _ = make(
        config, store, [[quote(2.00)], [quote(1.80)], [quote(1.80)]], telegram=OfflineTelegram()
    )
    watcher.poll_once(at(20))
    assert len(watcher.poll_once(at(5))) == 1
    watcher.telegram = FakeTelegram()
    assert len(watcher.poll_once(at(4))) == 1  # not marked as sent, so still alertable


def test_cli_command_credential_requirements_are_satisfiable():
    """Every command's declared requirements must actually build a Config."""
    from odds_watcher.cli import REQUIRED_CREDENTIALS
    from odds_watcher.config import ALL_CREDENTIALS, Config

    for command in ("run", "once", "check", "select-bookmakers", "chat-id", "status"):
        required = REQUIRED_CREDENTIALS.get(command, ALL_CREDENTIALS)
        env = {name: "value" for name in required}
        assert Config.from_env(env, required=required) is not None


def test_budget_warning_fires_when_the_slate_is_too_wide(config, capsys):
    """A wide slate must be flagged at setup, not discovered at 3am."""
    import dataclasses

    from odds_watcher.cli import _report_budget_fit

    now = 1_000_000.0
    wide = [
        Event(id=str(i), start_ts=now + 600 + i, home=f"H{i}", away="A") for i in range(40)
    ]
    _report_budget_fit(config, wide, now)
    out = capsys.readouterr().out
    assert "40 fixture(s)" in out
    assert "over your 90/hour cap" in out

    narrow = wide[:10]
    _report_budget_fit(dataclasses.replace(config), narrow, now)
    assert "over your" not in capsys.readouterr().out


def test_budget_warning_ignores_fixtures_outside_the_lead(config, capsys):
    from odds_watcher.cli import _report_budget_fit

    now = 1_000_000.0
    distant = [Event(id=str(i), start_ts=now + 6 * 3600, home=f"H{i}", away="A") for i in range(50)]
    _report_budget_fit(config, distant, now)
    assert "0 fixture(s)" in capsys.readouterr().out


def test_probe_calls_an_unpriced_slate_a_coverage_problem(config, capsys, monkeypatch):
    """Empty payloads mean the books do not quote these leagues — say so plainly."""
    from odds_watcher import cli

    class BlankApi:
        budget = None

        def get_events(self, sport, league=None, limit=None):
            return [
                Event(id="e1", start_ts=KICKOFF, home="Goianesia", away="Trindade",
                      league="Brazil - Goiano, 2. Divisao")
            ]

        def get_odds_payloads(self, ids, books, **kw):
            return [{"id": "e1", "urls": {}, "bookmakers": {}}]

    monkeypatch.setattr(cli, "_components", lambda cfg: (_FakeStore(), None, BlankApi(), None))
    monkeypatch.setattr(cli, "now_ts", lambda: KICKOFF - 600)

    assert cli.cmd_probe(config) == 1
    out = capsys.readouterr().out
    assert "no prices from any watched book" in out
    assert "coverage problem, not a parsing one" in out


def test_probe_reports_which_books_priced_each_fixture(config, capsys, monkeypatch):
    from odds_watcher import cli

    class MixedApi:
        budget = None

        def get_events(self, sport, league=None, limit=None):
            return [
                Event(id="e1", start_ts=KICKOFF, home="Arsenal", away="Chelsea", league="EPL"),
                Event(id="e2", start_ts=KICKOFF, home="Small", away="Club", league="Lower"),
            ]

        def get_odds_payloads(self, ids, books, **kw):
            return [
                {"id": "e1", "bookmakers": {"Bet365": [
                    {"name": "Totals", "odds": [{"hdp": 2.5, "over": "1.95", "under": "1.85"}]}]}},
                {"id": "e2", "bookmakers": {}},
            ]

    monkeypatch.setattr(cli, "_components", lambda cfg: (_FakeStore(), None, MixedApi(), None))
    monkeypatch.setattr(cli, "now_ts", lambda: KICKOFF - 600)

    assert cli.cmd_probe(config) == 0
    out = capsys.readouterr().out
    assert "✓ Arsenal vs Chelsea" in out
    assert "✗ Small vs Club" in out
    assert "1/2 sampled fixture(s) priced" in out
    assert "betano priced none" in out or "priced none of the sampled fixtures" in out


class _FakeStore:
    def close(self):
        pass


def test_discovery_sampling_spans_the_whole_horizon(config):
    """Sampling the next N fixtures only ever sees whichever region plays now."""
    from odds_watcher import cli

    now = 1_000_000.0

    class Feed:
        budget = None

        def get_events(self, sport, league=None, limit=None):
            # 100 fixtures over four days; the early ones are all one league.
            return [
                Event(
                    id=str(i),
                    start_ts=now + 600 + i * 3600,
                    home=f"H{i}",
                    away="A",
                    league="Lower" if i < 25 else "Major",
                )
                for i in range(100)
            ]

    nearest = cli._upcoming(Feed(), config, now, 10)
    assert {e.league for e in nearest} == {"Lower"}

    spread = cli._upcoming(Feed(), config, now, 10, spread=True)
    assert {e.league for e in spread} == {"Lower", "Major"}
    assert len(spread) == 10


def test_alerts_are_ranked_and_capped(config, store):
    """A prop-heavy poll must send the sharpest moves, not the first twenty."""
    import dataclasses

    from odds_watcher.detector import Alert

    cfg = dataclasses.replace(config, max_alerts_per_poll=3)
    watcher, _api, _tg = make(cfg, store, [[]])

    alerts = [
        Alert(event=EVENT, quote=quote(2.0), reference_odds=2.0, drop_pct=pct, seconds_to_start=300)
        for pct in (5.1, 22.0, 8.0, 13.5, 6.0)
    ]
    capped = watcher.rank_and_cap(alerts)
    assert [a.drop_pct for a in capped] == [22.0, 13.5, 8.0]


def test_cap_is_a_no_op_below_the_limit(config, store):
    from odds_watcher.detector import Alert

    watcher, _api, _tg = make(config, store, [[]])
    alerts = [
        Alert(event=EVENT, quote=quote(2.0), reference_odds=2.0, drop_pct=7.0, seconds_to_start=300)
    ]
    assert watcher.rank_and_cap(alerts) == alerts


def test_uncapped_alerts_are_not_marked_as_sent(config, store):
    """Only delivered alerts get marked, so a suppressed drop can alert later."""
    import dataclasses

    cfg = dataclasses.replace(config, max_alerts_per_poll=1)
    watcher, _api, telegram = make(
        cfg, store, [[quote(2.00), quote(2.00, bookmaker="betano")],
                     [quote(1.50), quote(1.80, bookmaker="betano")]]
    )
    watcher.poll_once(at(20))
    watcher.poll_once(at(5))

    # bet365 dropped 25%, betano 10% — only the larger is sent and marked.
    assert len(telegram.sent) == 1
    assert "BET365" in telegram.sent[0]
    assert store.get_state(quote(1.50)).alert_count == 1
    assert store.get_state(quote(1.80, bookmaker="betano")).alert_count == 0


def test_bad_sport_slug_is_explained_with_near_matches(config, capsys, monkeypatch):
    """"Baseball" is rejected where "baseball" works — say so, don't just 400."""
    import dataclasses

    from odds_watcher import cli
    from odds_watcher.http import HttpError

    class PickyApi:
        budget = None

        def get_leagues(self, sport):
            raise HttpError(400, '{"error":"Invalid sport slug"}', f"/leagues?sport={sport}")

        def get_sports(self):
            return [("baseball", "Baseball"), ("football", "Football")]

    monkeypatch.setattr(cli, "_components", lambda cfg: (_FakeStore(), None, PickyApi(), None))
    assert cli.cmd_leagues(dataclasses.replace(config, sports=("Baseball",))) == 1

    out = capsys.readouterr().out
    assert "slugs are lowercase" in out
    assert "did you mean, for 'Baseball': baseball" in out


class _PropsApi:
    """Batched response carries game markets; per-fixture adds player props."""

    budget = None

    def __init__(self, props=True):
        self.props = props
        self.per_event_calls = 0

    @staticmethod
    def _block(markets):
        return {"id": "m0", "bookmakers": {"Bet365": markets}}

    GAME = [{"name": "ML", "odds": [{"home": "1.85", "away": "1.95"}]},
            {"name": "Totals", "odds": [{"hdp": 8.5, "over": "1.90", "under": "1.90"}]}]
    PROPS = [{"name": "Player Strikeouts", "odds": [{"hdp": 5.5, "over": "1.9", "under": "1.9"}]}]

    def get_events(self, sport, league=None, limit=None):
        return [Event(id="m0", start_ts=KICKOFF, home="Yankees", away="Red Sox", league="USA - MLB")]

    def get_odds_payloads(self, ids, books, **kw):
        return [self._block(self.GAME)]

    def get_event_odds_raw(self, event_id, books):
        self.per_event_calls += 1
        return self._block(self.GAME + self.PROPS if self.props else self.GAME)


def test_props_check_detects_a_per_fixture_only_market(config, capsys, monkeypatch):
    from odds_watcher import cli

    api = _PropsApi(props=True)
    monkeypatch.setattr(cli, "_components", lambda cfg: (_FakeStore(), None, api, None))
    monkeypatch.setattr(cli, "now_ts", lambda: KICKOFF - 300)

    assert cli.cmd_props(config) == 0
    out = capsys.readouterr().out
    assert "Player Strikeouts" in out
    assert "PER_EVENT_ODDS=true" in out
    assert api.per_event_calls == 1


def test_props_check_says_so_when_there_are_none(config, capsys, monkeypatch):
    from odds_watcher import cli

    monkeypatch.setattr(cli, "_components", lambda cfg: (_FakeStore(), None, _PropsApi(props=False), None))
    monkeypatch.setattr(cli, "now_ts", lambda: KICKOFF - 300)

    assert cli.cmd_props(config) == 0
    out = capsys.readouterr().out
    assert "batching loses nothing" in out
    assert "Nothing that looks like a player-prop market" in out


def test_per_event_mode_requests_each_fixture_individually(config, store):
    """PER_EVENT_ODDS trades request budget for the markets only /odds returns."""
    import dataclasses

    class CountingApi(FakeApi):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.single_calls = []

        def get_event_odds(self, event_id, bookmakers, *, sport=""):
            self.single_calls.append((event_id, sport))
            return [quote(2.00, event_id=event_id)]

    other = Event(id="e3", start_ts=KICKOFF, home="Roma", away="Lazio")
    cfg = dataclasses.replace(config, per_event_odds=True)
    api = CountingApi([EVENT, other], [])
    watcher = Watcher(cfg, api, FakeTelegram(), store)

    watcher.poll_once(at(20))
    assert [call[0] for call in api.single_calls] == ["e1", "e3"]
    assert api.odds_calls == []  # never batched


def test_props_check_when_the_batched_endpoint_returns_more(config, capsys, monkeypatch):
    """The real MLB answer: batched carries props the per-fixture call omits."""
    from odds_watcher import cli

    class InvertedApi(_PropsApi):
        def get_odds_payloads(self, ids, books, **kw):
            return [self._block(self.GAME + self.PROPS)]

        def get_event_odds_raw(self, event_id, books):
            self.per_event_calls += 1
            return self._block(self.GAME)

    monkeypatch.setattr(cli, "_components", lambda cfg: (_FakeStore(), None, InvertedApi(), None))
    monkeypatch.setattr(cli, "now_ts", lambda: KICKOFF - 300)

    assert cli.cmd_props(config) == 0
    out = capsys.readouterr().out
    assert "only in the batched response (1)" in out
    assert "batched endpoint returns MORE" in out
    assert "PER_EVENT_ODDS=false" in out
    assert "batching loses nothing" not in out  # the bug this replaced
    assert "Player Strikeouts" in out


def test_prop_market_names_are_recognised_by_statistic():
    """Props are named by statistic, not by the word "prop"."""
    from odds_watcher.cli import _looks_like_a_prop

    for name in ("Pitcher Strikeouts O/U", "Pitcher Walks Issued O/U", "Home Runs O/U",
                 "Player Total Bases", "Batter Hits O/U"):
        assert _looks_like_a_prop(name), name

    for name in ("ML", "Run Line", "Totals", "First 5 Innings ML", "Team Total Away",
                 "Last Team To Score"):
        assert not _looks_like_a_prop(name), name


def test_listing_counts_match_what_is_printed(config, capsys, monkeypatch):
    """A "(12)" header followed by ten names is a reporting bug."""
    from odds_watcher import cli

    many = [{"name": f"Market {i}", "odds": [{"over": "1.9", "under": "1.9"}]} for i in range(30)]

    class WideApi(_PropsApi):
        def get_odds_payloads(self, ids, books, **kw):
            return [self._block(self.GAME + many)]

        def get_event_odds_raw(self, event_id, books):
            return self._block(self.GAME)

    monkeypatch.setattr(cli, "_components", lambda cfg: (_FakeStore(), None, WideApi(), None))
    monkeypatch.setattr(cli, "now_ts", lambda: KICKOFF - 300)
    cli.cmd_props(config)

    out = capsys.readouterr().out
    assert "only in the batched response (30)" in out
    assert "... and 5 more" in out  # 25 shown + 5 elided == 30


def test_watcher_runs_unchanged_against_the_odds_api(config, store):
    """The provider swap must not touch detection, storage or alerting."""
    import dataclasses
    import json
    from pathlib import Path

    from odds_watcher.theoddsapi import parse_quotes as v4_parse

    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "the_odds_api_v4.json").read_text(encoding="utf-8")
    )
    event = Event(id="e91d0d0d2b1c9a1f", start_ts=KICKOFF, home="Philadelphia Phillies",
                  away="St. Louis Cardinals", league="MLB")
    baseline = [q for q in v4_parse(raw) if q.event_id == event.id]

    dropped = json.loads(json.dumps(raw))
    dropped[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 1.45  # 1.65 -> 1.45
    moved = [q for q in v4_parse(dropped) if q.event_id == event.id]

    cfg = dataclasses.replace(config, bookmakers=("draftkings", "betfair_ex_uk"))
    api = FakeApi([event], [baseline, moved])
    telegram = FakeTelegram()
    watcher = Watcher(cfg, api, telegram, store)

    assert watcher.poll_once(at(20)) == []
    alerts = watcher.poll_once(at(5))
    assert len(alerts) == 1
    assert alerts[0].quote.market == "h2h"
    assert alerts[0].reference_odds == 1.65
    assert round(alerts[0].drop_pct, 1) == 12.1
    assert "DRAFTKINGS" in telegram.sent[0]


def test_exhausted_local_budget_is_a_message_not_a_traceback(config, capsys, monkeypatch):
    """The watcher's own cap firing must not look like a crash."""
    from odds_watcher import cli
    from odds_watcher.odds_api import BudgetExceeded

    class SpentApi:
        budget = None

        def get_bookmakers(self):
            raise BudgetExceeded("local budget cannot cover 3 credit(s) for sports/x/odds")

    monkeypatch.setattr(cli, "_components", lambda cfg: (_FakeStore(), None, SpentApi(), None))
    assert cli.cmd_bookmakers(config) == 1

    err = capsys.readouterr().err
    assert "local budget cannot cover" in err
    assert "watcher's own cap, not the provider's" in err
    assert "--reset-budget" in err


def test_leagues_falls_back_to_the_sports_listing(config, capsys, monkeypatch):
    """On The Odds API the sport key *is* the competition — answer, don't error."""
    import dataclasses

    from odds_watcher import cli
    from odds_watcher.theoddsapi import UnsupportedByProvider

    class V4Api:
        budget = None

        def get_leagues(self, sport):
            raise UnsupportedByProvider("The Odds API has no separate league list")

        def get_sports(self, include_all=False):
            assert include_all is True
            return [("baseball_mlb", "MLB (Baseball)"), ("soccer_epl", "EPL (Soccer)")]

    monkeypatch.setattr(cli, "_components", lambda cfg: (_FakeStore(), None, V4Api(), None))
    cfg = dataclasses.replace(config, odds_provider="the-odds-api")

    assert cli.cmd_leagues(cfg) == 0
    out = capsys.readouterr().out
    assert "no separate league list" in out
    assert "baseball_mlb" in out and "soccer_epl" in out


def test_fixtures_are_grouped_by_sport_for_odds(config, store):
    """A provider scoped by sport cannot serve a mixed batch."""
    import dataclasses

    mlb = Event(id="b1", start_ts=KICKOFF, home="Phillies", away="Cardinals", sport_key="baseball_mlb")
    epl = Event(id="s1", start_ts=KICKOFF, home="Arsenal", away="Chelsea", sport_key="soccer_epl")
    laliga = Event(id="s2", start_ts=KICKOFF, home="Real", away="Barca", sport_key="soccer_spain_la_liga")

    cfg = dataclasses.replace(config, sports=("baseball_mlb", "soccer_epl", "soccer_spain_la_liga"))
    api = FakeApi([mlb, epl, laliga], [[], [], []])
    watcher = Watcher(cfg, api, FakeTelegram(), store)
    watcher.poll_once(at(20))

    grouped = dict(zip(api.sports_asked, api.odds_calls))
    assert grouped["baseball_mlb"] == ["b1"]
    assert grouped["soccer_epl"] == ["s1"]
    assert grouped["soccer_spain_la_liga"] == ["s2"]


def test_events_are_tagged_with_the_sport_they_came_from(config, store):
    """Providers that do not name the sport in the payload still need it."""
    import dataclasses

    untagged = Event(id="x1", start_ts=KICKOFF, home="A", away="B")  # no sport_key

    class PerSportApi(FakeApi):
        def get_events(self, sport, league=None, limit=None):
            self.event_calls += 1
            return [dataclasses.replace(untagged, id=f"{sport}-1")]

    cfg = dataclasses.replace(config, sports=("baseball_mlb", "soccer_epl"))
    api = PerSportApi([], [[], []])
    watcher = Watcher(cfg, api, FakeTelegram(), store)
    watcher.refresh_events(at(20))

    assert {e.sport_key for e in watcher._events} == {"baseball_mlb", "soccer_epl"}


def test_usage_reports_burn_rate_against_the_account_balance(config, capsys, monkeypatch, tmp_path):
    """"How fast am I spending" needs local spend and the provider's balance."""
    import dataclasses

    from odds_watcher import cli
    from odds_watcher.store import RequestBudget, Store
    from odds_watcher.util import now_ts

    store = Store(tmp_path / "usage.db")
    store.add_call(now_ts() - 60, cost=250)

    class QuotaApi:
        budget = None

        def fetch_quota(self):
            return {"remaining": 1000, "used": 19000, "last_call": 3}

    cfg = dataclasses.replace(config, odds_provider="the-odds-api", max_requests_per_hour=500)
    budget = RequestBudget(store, cfg.max_requests_per_hour, cfg.max_requests_per_day)
    monkeypatch.setattr(cli, "_components", lambda c: (store, budget, QuotaApi(), None))

    assert cli.cmd_usage(cfg) == 0
    out = capsys.readouterr().out
    assert "spent last hour: 250 credits" in out
    assert "1000 remaining" in out
    assert "250/hour -> about 4.0 hour(s) of headroom" in out
    assert "runs dry within a day" in out


def test_usage_without_a_metered_provider(config, capsys, monkeypatch, tmp_path):
    from odds_watcher import cli
    from odds_watcher.store import RequestBudget, Store

    store = Store(tmp_path / "usage2.db")
    budget = RequestBudget(store, config.max_requests_per_hour, config.max_requests_per_day)

    class PlainApi:
        budget = None

    monkeypatch.setattr(cli, "_components", lambda c: (store, budget, PlainApi(), None))
    assert cli.cmd_usage(config) == 0
    out = capsys.readouterr().out
    assert "spent last hour: 0 requests" in out
    assert "account credits" not in out


def test_all_sports_expands_from_the_provider_listing(config, store):
    """SPORTS=all polls whatever the provider lists, refreshed daily."""
    import dataclasses

    class Catalogue(FakeApi):
        def __init__(self):
            super().__init__([], [])
            self.sport_calls = 0

        def get_sports(self, include_all=False):
            self.sport_calls += 1
            return [("baseball_mlb", "MLB"), ("soccer_epl", "EPL"), ("icehockey_nhl", "NHL")]

        def get_events(self, sport, league=None, limit=None):
            self.event_calls += 1
            return [Event(id=f"{sport}-1", start_ts=KICKOFF, home="H", away="A", sport_key=sport)]

    api = Catalogue()
    cfg = dataclasses.replace(config, sports=("all",))
    watcher = Watcher(cfg, api, FakeTelegram(), store)

    watcher.refresh_events(at(30))
    assert {e.sport_key for e in watcher._events} == {"baseball_mlb", "soccer_epl", "icehockey_nhl"}
    assert api.sport_calls == 1

    # The listing is cached for a day rather than re-fetched every refresh.
    watcher.refresh_events(at(30) + config.events_refresh_seconds + 1)
    assert api.sport_calls == 1


def test_all_sports_survives_a_failed_listing(config, store):
    """A failed listing must not wipe the sports already being watched."""
    import dataclasses

    from odds_watcher.http import TransportError

    class Flaky(FakeApi):
        def __init__(self):
            super().__init__([], [])
            self.fail = False

        def get_sports(self, include_all=False):
            if self.fail:
                raise TransportError("listing unavailable")
            return [("baseball_mlb", "MLB")]

        def get_events(self, sport, league=None, limit=None):
            return []

    api = Flaky()
    watcher = Watcher(dataclasses.replace(config, sports=("all",)), api, FakeTelegram(), store)
    assert watcher.resolve_sports(at(30)) == ("baseball_mlb",)

    api.fail = True
    watcher._sports_fetched_at = 0  # force a refresh
    assert watcher.resolve_sports(at(20)) == ("baseball_mlb",)


def test_explicit_sports_never_call_the_listing(config, store):
    class NoListing(FakeApi):
        def get_sports(self, include_all=False):
            raise AssertionError("should not list sports when SPORTS is explicit")

    watcher = Watcher(config, NoListing([], []), FakeTelegram(), store)
    assert watcher.resolve_sports(at(30)) == config.sports
