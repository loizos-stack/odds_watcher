"""End-to-end behaviour of one poll cycle, with the network faked out."""

import dataclasses

from odds_watcher.http import HttpError
from odds_watcher.odds_api import Event, Quote
from odds_watcher.watcher import Watcher, placeholder_start_times

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
    assert "Ajax vs PSV" in body and "Bet365" in body and "[-10.0%]" in body


def test_events_are_only_refetched_when_stale(config, store):
    watcher, api, _ = make(config, store, [[], [], []])
    watcher.poll_once(at(30))
    watcher.poll_once(at(29))
    assert api.event_calls == 1
    watcher.poll_once(at(29) + config.events_refresh_seconds + 1)
    assert api.event_calls == 2


HOUR_TS = 1_700_000_000.0 // 3600 * 3600  # an exact UTC hour boundary


def test_placeholder_start_times_flags_an_on_the_hour_cluster():
    # A dozen games stamped with the identical on-the-hour default...
    fake = [Event(id=str(i), start_ts=HOUR_TS, home=f"H{i}", away=f"A{i}") for i in range(12)]
    # ...alongside real, staggered first pitches that must survive.
    real = [
        Event(id="r1", start_ts=HOUR_TS + 305, home="Real1", away="X"),
        Event(id="r2", start_ts=HOUR_TS + 4238, home="Real2", away="Y"),
    ]
    flagged = placeholder_start_times(fake + real, min_cluster=6)
    assert flagged == {HOUR_TS}


def test_placeholder_check_spares_a_real_simultaneous_wave():
    # Seven genuine games at :05 share a time but are not on the hour.
    wave = [Event(id=str(i), start_ts=HOUR_TS + 300, home=f"H{i}", away="A") for i in range(7)]
    assert placeholder_start_times(wave, min_cluster=6) == set()


def test_placeholder_check_is_disabled_by_zero():
    fake = [Event(id=str(i), start_ts=HOUR_TS, home=f"H{i}", away="A") for i in range(12)]
    assert placeholder_start_times(fake, min_cluster=0) == set()


def test_refresh_events_drops_placeholder_fixtures(config, store):
    cfg = dataclasses.replace(config, placeholder_min_cluster=6)
    phantom = [Event(id=f"p{i}", start_ts=HOUR_TS, home=f"H{i}", away="A") for i in range(8)]
    real = Event(id="real", start_ts=HOUR_TS + 305, home="Real", away="Team")
    watcher, _api, _tg = make(cfg, store, [[]], events=(*phantom, real))
    events = watcher.refresh_events(HOUR_TS - 600, force=True)
    ids = {e.id for e in events}
    assert ids == {"real"}  # the eight phantom fixtures are gone


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
    assert len(telegram.sent) == 2  # one message per drop
    joined = "\n".join(telegram.sent)
    assert "Ajax vs PSV" in joined and "Roma vs Lazio" in joined


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
    assert "Bet365" in telegram.sent[0]
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
    assert "DraftKings" in telegram.sent[0]


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


def test_empty_sports_listing_is_loud(config, store, caplog):
    """Watching nothing looks identical to a quiet night; say so."""
    import dataclasses
    import logging

    class EmptyCatalogue(FakeApi):
        def get_sports(self, include_all=False):
            return []

    watcher = Watcher(
        dataclasses.replace(config, sports=("all",)), EmptyCatalogue([], []), FakeTelegram(), store
    )
    with caplog.at_level(logging.ERROR):
        assert watcher.resolve_sports(at(30)) == ()
    assert "resolved to no sports" in caplog.text


def test_balance_check_is_opt_in_when_it_costs_a_request(config, capsys, monkeypatch, tmp_path):
    """Spending quota to find out how much quota is left must be deliberate."""
    import dataclasses

    from odds_watcher import cli
    from odds_watcher.store import RequestBudget, Store

    class Metered:
        budget = None
        calls = 0

        def fetch_quota(self):
            type(self).calls += 1
            return {"remaining": 520}

    def components(cfg_):
        # A fresh store per invocation, as the real factory does.
        store = Store(tmp_path / "optin.db")
        budget = RequestBudget(store, cfg_.max_requests_per_hour, cfg_.max_requests_per_day)
        return store, budget, Metered(), None

    cfg = dataclasses.replace(config, odds_provider="parlay-api")
    monkeypatch.setattr(cli, "_components", components)

    cli.cmd_usage(cfg)
    assert Metered.calls == 0
    assert "--check-balance" in capsys.readouterr().out

    cli.cmd_usage(cfg, check_balance=True)
    assert Metered.calls == 1
    assert "520 remaining" in capsys.readouterr().out


def test_balance_check_is_automatic_when_it_is_free(config, capsys, monkeypatch, tmp_path):
    """The Odds API reports the balance on a free endpoint, so always read it."""
    import dataclasses

    from odds_watcher import cli
    from odds_watcher.store import RequestBudget, Store

    store = Store(tmp_path / "free.db")
    budget = RequestBudget(store, config.max_requests_per_hour, config.max_requests_per_day)

    class FreeQuota:
        budget = None

        def fetch_quota(self):
            return {"remaining": 19000, "used": 1000}

    monkeypatch.setattr(cli, "_components", lambda c: (store, budget, FreeQuota(), None))
    cli.cmd_usage(dataclasses.replace(config, odds_provider="the-odds-api"))
    assert "19000 remaining" in capsys.readouterr().out


def test_unknown_balance_is_said_out_loud(config, capsys, monkeypatch, tmp_path):
    """A balance check that answers nothing must not print nothing."""
    import dataclasses

    from odds_watcher import cli
    from odds_watcher.store import RequestBudget, Store

    class Silent:
        budget = None

        def fetch_quota(self):
            return {"remaining": None}

    def components(cfg_):
        store = Store(tmp_path / "silent.db")
        return store, RequestBudget(store, 100, 100), Silent(), None

    monkeypatch.setattr(cli, "_components", components)
    cli.cmd_usage(dataclasses.replace(config, odds_provider="parlay-api"), check_balance=True)

    out = capsys.readouterr().out
    assert "did not report a remaining allowance" in out
    assert "not the provider's" in out


def test_sport_hint_does_not_fire_on_every_sports_url(config, capsys, monkeypatch):
    """Every endpoint path contains "/sports/"; that is not a bad slug."""
    from odds_watcher import cli
    from odds_watcher.http import HttpError

    class Api:
        budget = None

        def get_sports(self, include_all=False):
            raise AssertionError("should not look up sports for a market error")

    market_error = HttpError(
        400, '{"error":"INVALID_MARKET"}', "https://parlay-api.com/v1/sports/baseball_mlb/odds"
    )
    cli._sport_error(Api(), config, market_error)
    assert capsys.readouterr().out == ""

    slug_error = HttpError(400, '{"error":"Invalid sport slug"}', "https://x/v1/sports/Baseball/odds")

    class Listing(Api):
        def get_sports(self, include_all=False):
            return [("baseball_mlb", "MLB")]

    import dataclasses

    cli._sport_error(Listing(), dataclasses.replace(config, sports=("Baseball",)), slug_error)
    assert "slugs are lowercase" in capsys.readouterr().out


def test_movements_explains_stranded_lines(config, capsys, monkeypatch, tmp_path):
    """The report must name the reason a large drop produced no signal."""
    import dataclasses

    from odds_watcher import cli
    from odds_watcher.odds_api import Quote
    from odds_watcher.store import RequestBudget, Store

    store = Store(tmp_path / "stranded.db")
    q = Quote("e1", "bet365", "h2h", "", "Home", 3.00)
    store.record(q, pre_window=False, event_start=1000, ts=1, event_name="C vs D")
    store.record(Quote("e1", "bet365", "h2h", "", "Home", 2.40),
                 pre_window=False, event_start=1000, ts=2)
    store.commit()

    monkeypatch.setattr(
        cli, "_components",
        lambda c: (store, RequestBudget(store, 100, 100), None, None),
    )
    assert cli.cmd_movements(dataclasses.replace(config, min_drop_pct=2.0)) == 0

    out = capsys.readouterr().out
    assert "0 of 1 line(s) have a pre-window reference" in out
    assert "no pre-window price" in out
    assert "cannot signal" in out
    assert "BASELINE_MODE=first-seen" in out


def test_movements_does_not_claim_stranded_lines_in_first_seen_mode(config, capsys,
                                                                    monkeypatch, tmp_path):
    """first-seen takes the first price as the reference, so nothing is stranded."""
    import dataclasses

    from odds_watcher import cli
    from odds_watcher.odds_api import Quote
    from odds_watcher.store import RequestBudget, Store

    store = Store(tmp_path / "firstseen.db")
    store.record(Quote("e1", "dk", "batter_home_runs", "0.5", "Judge Over", 3.60),
                 pre_window=False, event_start=1000, ts=1, event_name="A vs B")
    store.record(Quote("e1", "dk", "batter_home_runs", "0.5", "Judge Over", 2.50),
                 pre_window=False, event_start=1000, ts=2)
    store.mark_alerted(Quote("e1", "dk", "batter_home_runs", "0.5", "Judge Over", 2.50), ts=2)

    monkeypatch.setattr(
        cli, "_components", lambda c: (store, RequestBudget(store, 100, 100), None, None)
    )
    cli.cmd_movements(dataclasses.replace(config, baseline_mode="first-seen"))

    out = capsys.readouterr().out
    assert "every tracked line can signal" in out
    assert "no pre-window price" not in out
    assert "cannot signal" not in out
    assert "SIGNALLED" in out


def test_poll_cost_counts_only_the_sports_with_a_fixture_in_range(config, store, caplog):
    """89 configured sports do not cost 89 odds requests a poll — only those in range do."""
    import dataclasses
    import logging

    class Balance(FakeApi):
        credits_remaining = 858

    cfg = dataclasses.replace(
        config,
        prop_markets=("all",),
        poll_interval_seconds=300,
        events_refresh_seconds=3600,
        sports=tuple(f"sport{i}" for i in range(89)),
    )
    watcher = Watcher(cfg, Balance([], []), FakeTelegram(), store)
    # One fixture inside the lead, one far away: only the first sport is billed.
    watcher._events = [EVENT, dataclasses.replace(FAR_EVENT, sport_key="sport2")]
    watcher._events[0] = dataclasses.replace(EVENT, sport_key="sport1")

    with caplog.at_level(logging.INFO):
        watcher.warn_if_unaffordable(KICKOFF - 300)

    # 1 sport in range x (odds + props) = 2 per poll, not 178.
    assert watcher.estimate_poll_cost(KICKOFF - 300) == 2
    assert watcher.estimate_refresh_cost(89) == 89
    # 2 per poll x 12 polls/hour + 89 per refresh x 1 refresh/hour = 113.
    assert "~113 request(s)/hour in total" in caplog.text
    assert "858 lasts about 7.6 hour(s)" in caplog.text
    assert "exceeds MAX_REQUESTS_PER_HOUR" in caplog.text


def test_refresh_cost_multiplies_by_league(config, store):
    """The fixture list is fetched per sport per league, so leagues multiply it."""
    import dataclasses

    cfg = dataclasses.replace(config, leagues=("epl", "laliga", "seriea"))
    watcher = Watcher(cfg, FakeApi([], []), FakeTelegram(), store)
    assert watcher.estimate_refresh_cost(4) == 12


def test_no_alarm_when_the_scope_fits(config, store, caplog):
    import dataclasses
    import logging

    cfg = dataclasses.replace(config, prop_markets=(), poll_interval_seconds=300,
                              events_refresh_seconds=900, max_requests_per_hour=100)
    watcher = Watcher(cfg, FakeApi([], []), FakeTelegram(), store)
    with caplog.at_level(logging.WARNING):
        watcher.warn_if_unaffordable(KICKOFF - 300)
    assert "exceeds MAX_REQUESTS_PER_HOUR" not in caplog.text


def test_a_sport_scoped_provider_is_asked_once_per_sport(config, store):
    """ParlayAPI returns the whole sport per call; batching buys it twice."""
    import dataclasses

    class SportScoped(FakeApi):
        sport_scoped_odds = True

    events = [
        dataclasses.replace(EVENT, id=f"e{i}", sport_key="soccer")
        for i in range(45)  # more than two batches of EVENTS_PER_ODDS_REQUEST
    ]
    api = SportScoped(events, [])
    watcher = Watcher(config, api, FakeTelegram(), store)
    watcher.poll_once(KICKOFF - 300)

    assert len(api.odds_calls) == 1
    assert len(api.odds_calls[0]) == 45
    assert watcher.estimate_poll_cost(KICKOFF - 300) == 1


def test_a_per_event_provider_still_batches(config, store):
    """Providers that honour event ids keep the 20-per-request batching."""
    import dataclasses

    events = [dataclasses.replace(EVENT, id=f"e{i}", sport_key="soccer") for i in range(45)]
    api = FakeApi(events, [])
    watcher = Watcher(config, api, FakeTelegram(), store)
    watcher.poll_once(KICKOFF - 300)

    assert len(api.odds_calls) == 3  # 20 + 20 + 5
    assert watcher.estimate_poll_cost(KICKOFF - 300) == 3


def test_the_estimate_counts_props_only_where_they_are_fetched(config, store):
    """PROP_SPORTS is the lever that makes props affordable; the estimate must see it."""
    import dataclasses

    class Scoped(FakeApi):
        sport_scoped_odds = True

        def wants_props(self, sport):
            return sport == "baseball_mlb"

    cfg = dataclasses.replace(config, prop_markets=("all",),
                              prop_sports=("baseball_mlb",))
    watcher = Watcher(cfg, Scoped([], []), FakeTelegram(), store)
    watcher._events = [
        dataclasses.replace(EVENT, id="a", sport_key="baseball_mlb"),
        dataclasses.replace(EVENT, id="b", sport_key="table_tennis"),
    ]
    # mlb: odds + props = 2, table tennis: odds only = 1.
    assert watcher.estimate_poll_cost(KICKOFF - 300) == 3


def test_a_truncated_fixture_list_is_not_cached_for_the_full_interval(config, store, caplog):
    """A refresh the budget cuts short hides whole sports; it must retry soon."""
    import dataclasses
    import logging

    from odds_watcher.odds_api import BudgetExceeded

    class Starved(FakeApi):
        def __init__(self):
            super().__init__([EVENT], [])
            self.calls = 0

        def get_events(self, sport, league=None, limit=None):
            self.calls += 1
            if self.calls > 2:
                raise BudgetExceeded("local request budget exhausted")
            return [dataclasses.replace(EVENT, id=f"e{self.calls}", sport_key=sport)]

    cfg = dataclasses.replace(config, sports=("a", "b", "c", "d"),
                              events_refresh_seconds=86400)
    watcher = Watcher(cfg, Starved(), FakeTelegram(), store)

    with caplog.at_level(logging.ERROR):
        watcher.refresh_events(1000.0)

    assert watcher._events_partial
    assert "fixture list is INCOMPLETE" in caplog.text
    assert "2 of 4 sport(s)" in caplog.text

    # An hour later a full-interval cache would still be serving the half list.
    watcher.api = FakeApi([EVENT], [])
    watcher.refresh_events(1000.0 + 3600)
    assert not watcher._events_partial


def test_a_complete_refresh_is_cached_for_the_full_interval(config, store):
    import dataclasses

    cfg = dataclasses.replace(config, sports=("a",), events_refresh_seconds=86400)
    api = FakeApi([EVENT], [])
    watcher = Watcher(cfg, api, FakeTelegram(), store)

    watcher.refresh_events(1000.0)
    watcher.refresh_events(1000.0 + 3600)
    assert api.event_calls == 1          # served from cache, as intended
    assert not watcher._events_partial


def test_the_fixture_list_survives_a_restart(config, tmp_path):
    """Re-listing every sport on each restart is the costliest thing it can do."""
    import dataclasses

    from odds_watcher.store import Store

    cfg = dataclasses.replace(config, db_path=tmp_path / "restart.db",
                              events_refresh_seconds=86400)
    store = Store(cfg.db_path)
    api = FakeApi([EVENT], [])
    Watcher(cfg, api, FakeTelegram(), store, clock=lambda: 1000.0).refresh_events(1000.0)
    store.close()
    assert api.event_calls == 1

    # A fresh process, as after `systemctl restart`.
    store2 = Store(cfg.db_path)
    api2 = FakeApi([EVENT], [])
    watcher = Watcher(cfg, api2, FakeTelegram(), store2, clock=lambda: 1000.0 + 60)
    watcher.refresh_events(1000.0 + 60)

    assert api2.event_calls == 0            # nothing re-bought
    assert [e.id for e in watcher._events] == [EVENT.id]
    store2.close()


def test_a_restored_partial_list_still_retries_soon(config, tmp_path):
    """An incomplete list must not become permanent by surviving a restart."""
    import dataclasses

    from odds_watcher.store import Store

    cfg = dataclasses.replace(config, db_path=tmp_path / "partial.db",
                              events_refresh_seconds=86400)
    store = Store(cfg.db_path)
    scope = "|".join((",".join(cfg.sports), ",".join(cfg.leagues)))
    store.save_fixtures([EVENT], 1000.0, partial=True, scope=scope)
    store.close()

    store2 = Store(cfg.db_path)
    api = FakeApi([EVENT], [])
    watcher = Watcher(cfg, api, FakeTelegram(), store2, clock=lambda: 1000.0 + 700)
    assert watcher._events_partial
    watcher.refresh_events(1000.0 + 700)    # past PARTIAL_REFRESH_RETRY_SECONDS
    assert api.event_calls == 1
    store2.close()


def test_an_incomplete_list_is_not_left_broken_for_a_whole_idle_interval(config, store):
    """The repair happens inside a poll, so the sleep must not outlast it."""
    import dataclasses

    from odds_watcher.watcher import PARTIAL_REFRESH_RETRY_SECONDS

    cfg = dataclasses.replace(config, idle_poll_interval_seconds=900,
                              poll_interval_seconds=600)
    watcher = Watcher(cfg, FakeApi([], []), FakeTelegram(), store)
    watcher._events = [FAR_EVENT]           # nothing in range

    watcher._events_partial = False
    assert watcher.seconds_until_next_poll(KICKOFF) == 900

    watcher._events_partial = True
    assert watcher.seconds_until_next_poll(KICKOFF) == PARTIAL_REFRESH_RETRY_SECONDS


def test_every_poll_says_what_it_looked_at(config, store, caplog):
    """A silent poll makes a working watcher indistinguishable from a stuck one."""
    import logging

    api = FakeApi([EVENT], [[quote(2.00)]])
    watcher = Watcher(config, api, FakeTelegram(), store)
    with caplog.at_level(logging.INFO):
        watcher.poll_once(at(20))

    assert "polled 1 sport(s), 1 fixture(s): 1 price(s)" in caplog.text
    assert "1 line(s) tracked" in caplog.text
    assert "0 drop(s) >= 5.0%" in caplog.text


def test_fixtures_in_range_with_no_prices_is_called_out(config, store, caplog):
    """An empty payload is what a wrong book or market key looks like."""
    import logging

    watcher = Watcher(config, FakeApi([EVENT], [[]]), FakeTelegram(), store)
    with caplog.at_level(logging.WARNING):
        watcher.poll_once(at(20))

    assert "no prices came back" in caplog.text
    # The sports must be named: an unpriced sport and a broken request are
    # indistinguishable until you can see which sports were asked about.
    assert "football" in caplog.text
    assert "bet365" in caplog.text


def test_a_poll_with_nothing_in_range_stays_quiet(config, store, caplog):
    import logging

    watcher = Watcher(config, FakeApi([FAR_EVENT], []), FakeTelegram(), store)
    with caplog.at_level(logging.INFO):
        watcher.poll_once(at(600))
    assert "polled" not in caplog.text


def test_changing_sports_invalidates_the_cached_fixture_list(config, tmp_path):
    """Otherwise it keeps polling sports you removed and misses ones you added."""
    import dataclasses

    from odds_watcher.store import Store

    db = tmp_path / "scope.db"
    wide = dataclasses.replace(config, sports=("a", "b", "c"), db_path=db,
                               events_refresh_seconds=86400)
    store = Store(db)
    Watcher(wide, FakeApi([EVENT], []), FakeTelegram(), store).refresh_events(1000.0)
    store.close()

    narrow = dataclasses.replace(wide, sports=("a",))
    store2 = Store(db)
    api = FakeApi([EVENT], [])
    watcher = Watcher(narrow, api, FakeTelegram(), store2, clock=lambda: 1060.0)
    assert watcher._events == []             # not reused across a scope change
    watcher.refresh_events(1060.0)
    assert api.event_calls == 1
    store2.close()


def test_the_same_sports_still_reuse_the_cache(config, tmp_path):
    import dataclasses

    from odds_watcher.store import Store

    db = tmp_path / "samescope.db"
    cfg = dataclasses.replace(config, sports=("a", "b"), db_path=db,
                              events_refresh_seconds=86400)
    store = Store(db)
    Watcher(cfg, FakeApi([EVENT], []), FakeTelegram(), store).refresh_events(1000.0)
    store.close()

    store2 = Store(db)
    api = FakeApi([EVENT], [])
    watcher = Watcher(cfg, api, FakeTelegram(), store2, clock=lambda: 1060.0)
    watcher.refresh_events(1060.0)
    assert api.event_calls == 0
    store2.close()


def test_prices_for_unknown_fixtures_are_reported_not_swallowed(config, store, caplog):
    """A price with no matching fixture cannot be timed, and used to vanish."""
    import logging

    from odds_watcher.odds_api import Quote

    stray = Quote("not-in-the-list", "bet365", "h2h", "", "Home", 2.0)
    api = FakeApi([EVENT], [[quote(2.00), stray]])
    watcher = Watcher(config, api, FakeTelegram(), store)
    with caplog.at_level(logging.WARNING):
        watcher.poll_once(at(20))

    assert "1 of 2 price(s) belong to fixtures not in the list" in caplog.text
    assert "1 price(s) were usable" in caplog.text


def test_digest_accumulates_and_goes_out_hourly(config, store):
    """The digest collects drops and emits one summary after the interval."""
    import dataclasses

    cfg = dataclasses.replace(config, digest_interval_seconds=3600, min_drop_pct=5.0,
                              baseline_mode="last-seen", window_start_seconds=1200)
    tg = FakeTelegram()
    watcher = Watcher(cfg, FakeApi([EVENT], []), tg, store)

    # Drops accumulate for the digest; the digest itself waits for the hour.
    watcher.detector.process(EVENT, [quote(2.00)], at(18))
    a1 = watcher.detector.process(EVENT, [quote(1.70)], at(16))
    watcher.add_to_digest(a1, at(16))
    watcher.flush_digest_if_due(at(16))
    assert tg.sent == []                       # nothing from the digest yet

    a2 = watcher.detector.process(EVENT, [quote(1.50)], at(14))
    watcher.add_to_digest(a2, at(14))
    watcher.flush_digest_if_due(at(14))
    assert tg.sent == []

    # An hour after the first buffered alert, one summary goes out.
    watcher.flush_digest_if_due(at(16) + 3601)
    assert len(tg.sent) == 1
    assert "Summary" in tg.sent[0]
    assert watcher._digest == []


def test_immediate_mode_is_unchanged_when_interval_is_zero(config, store):
    tg = FakeTelegram()
    watcher = Watcher(config, FakeApi([EVENT], [[quote(2.00)], [quote(1.70)]]), tg, store)
    watcher.poll_once(at(20))   # baseline
    watcher.poll_once(at(8))    # drop -> sent right away
    assert len(tg.sent) == 1
    assert watcher._digest == []


def test_digest_and_per_drop_alerts_both_fire(config, store):
    """A digest interval adds the summary; it does not replace instant alerts."""
    import dataclasses

    cfg = dataclasses.replace(config, digest_interval_seconds=3600, min_drop_pct=5.0)
    tg = FakeTelegram()
    watcher = Watcher(cfg, FakeApi([EVENT], [[quote(2.00)], [quote(1.70)]]), tg, store)
    watcher.poll_once(at(20))   # baseline
    watcher.poll_once(at(8))    # a drop: sent immediately AND buffered

    assert len(tg.sent) == 1                     # the per-drop message
    assert len(watcher._digest) == 1             # also queued for the hour
    watcher.flush_digest_if_due(at(8) + 3601)
    assert len(tg.sent) == 2                     # now the hourly summary too
    assert "Summary" in tg.sent[1]


def test_digest_goes_to_its_own_chat_when_set(config, store):
    """The hourly digest can target a different Telegram chat than the alerts."""
    import dataclasses

    class ChatAwareTelegram:
        def __init__(self):
            self.sent = []

        def send_message(self, text, chat_id=None, **kw):
            self.sent.append((chat_id, text))
            return {"ok": True}

    cfg = dataclasses.replace(config, digest_interval_seconds=3600, min_drop_pct=5.0,
                              digest_chat_id="99887766")
    tg = ChatAwareTelegram()
    watcher = Watcher(cfg, FakeApi([EVENT], [[quote(2.00)], [quote(1.70)]]), tg, store)
    watcher.poll_once(at(20))
    watcher.poll_once(at(8))       # per-drop alert -> default chat (chat_id None)
    watcher.flush_digest_if_due(at(8) + 3601)

    per_drop = [c for c, _ in tg.sent if c is None]
    digest = [c for c, _ in tg.sent if c == "99887766"]
    assert per_drop and digest            # both went out, to different chats
