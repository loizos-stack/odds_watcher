"""The diagnostic commands must not misreport a working configuration."""

import dataclasses

import pytest

from odds_watcher import cli
from odds_watcher.odds_api import Event
from odds_watcher.store import RequestBudget, Store


class FakeApi:
    account_bookmaker_selection = False

    def __init__(self, sports=(("baseball_mlb", "MLB"),), events=()):
        self._sports = list(sports)
        self._events = list(events)
        self.asked = []

    def get_sports(self):
        return list(self._sports)

    def get_events(self, sport, league=None, limit=None):
        self.asked.append(sport)
        return list(self._events)

    def get_selected_bookmakers(self):
        return []


class FakeTelegram:
    def get_me(self):
        return {"result": {"username": "bot"}}

    def send_message(self, text, **kwargs):
        return {"ok": True}


@pytest.fixture
def wired(config, tmp_path, monkeypatch):
    """cmd_check with its components replaced, returning the fake api."""
    def install(api):
        store = Store(tmp_path / "check.db")
        monkeypatch.setattr(
            cli, "_components",
            lambda c: (store, RequestBudget(store, 100, 500), api, FakeTelegram()),
        )
        return api
    return install


def test_all_is_expanded_before_the_events_endpoint_is_called(config, wired, capsys):
    """SPORTS=all is not a sport key; sending it verbatim fails a valid config."""
    api = wired(FakeApi(sports=[("baseball_mlb", "MLB")],
                        events=[Event(id="e1", start_ts=9e9, home="A", away="B")]))
    cli.cmd_check(dataclasses.replace(config, sports=("all",)))

    assert api.asked == ["baseball_mlb"]
    assert "all event(s)" not in capsys.readouterr().out


def test_an_explicit_sport_is_used_as_given(config, wired):
    api = wired(FakeApi(events=[Event(id="e1", start_ts=9e9, home="A", away="B")]))
    cli.cmd_check(dataclasses.replace(config, sports=("soccer_epl",)))
    assert api.asked == ["soccer_epl"]


def test_no_account_selection_is_not_reported_as_a_problem(config, wired, capsys):
    """ParlayAPI picks books per request, so an empty selection is correct."""
    wired(FakeApi(events=[Event(id="e1", start_ts=9e9, home="A", away="B")]))
    cli.cmd_check(dataclasses.replace(config, odds_provider="parlay-api"))

    out = capsys.readouterr().out
    assert "select-bookmakers" not in out
    assert "not selected on the account" not in out
    assert "parlay-api chooses books per request" in out


def test_an_account_provider_still_warns_about_unselected_books(config, wired, capsys):
    class Account(FakeApi):
        account_bookmaker_selection = True

    wired(Account(events=[Event(id="e1", start_ts=9e9, home="A", away="B")]))
    cli.cmd_check(dataclasses.replace(config, odds_provider="odds-api-io",
                                      bookmakers=("bet365",)))
    assert "not selected on the account" in capsys.readouterr().out
