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
        self.budget = None

    def get_events(self, sport, league=None, limit=None):
        self.event_calls += 1
        return list(self.events)

    def get_multi_odds(self, event_ids, bookmakers):
        self.odds_calls.append(list(event_ids))
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
        def get_multi_odds(self, event_ids, bookmakers):
            raise HttpError(503, "upstream down", "https://api2.odds-api.io/v3/odds/multi")

    watcher = Watcher(config, BrokenApi([EVENT], []), FakeTelegram(), store)
    assert watcher.poll_once(at(5)) == []


def test_network_failure_is_handled_like_any_other_outage(config, store):
    """A dropped connection must not escape as a traceback (cron `once` mode)."""
    from odds_watcher.http import TransportError

    class OfflineApi(FakeApi):
        def get_multi_odds(self, event_ids, bookmakers):
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
