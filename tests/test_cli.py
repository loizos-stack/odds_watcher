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

    def get_bookmakers(self):
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


def test_unknown_bookmakers_are_caught_before_they_silence_the_watcher(config, wired, capsys):
    """A bad book key fails every odds call, and the only symptom is silence."""
    class Books(FakeApi):
        def get_bookmakers(self):
            return [("bet365", "Bet365"), ("draftkings", "DraftKings")]

    wired(Books(events=[Event(id="e1", start_ts=9e9, home="A", away="B")]))
    rc = cli.cmd_check(dataclasses.replace(
        config, odds_provider="parlay-api", bookmakers=("Bet365", "Betano")))

    out, err = capsys.readouterr()
    assert rc == 1
    assert "does not know: Betano" in err
    assert "nothing resembling 'Betano'" in out
    assert "Bet365" not in err.replace("Betano", "")   # the valid one is not flagged


def test_correct_bookmakers_are_confirmed(config, wired, capsys):
    class Books(FakeApi):
        def get_bookmakers(self):
            return [("bet365", "Bet365"), ("draftkings", "DraftKings")]

    wired(Books(events=[Event(id="e1", start_ts=9e9, home="A", away="B")]))
    cli.cmd_check(dataclasses.replace(
        config, odds_provider="parlay-api", bookmakers=("Bet365", "DraftKings")))
    assert "accepts all 2 book(s)" in capsys.readouterr().out


class TwoPassApi(FakeApi):
    """Returns a different price list on each odds call."""

    def __init__(self, passes, events=()):
        super().__init__(events=events)
        self.passes = list(passes)
        self.odds_calls = 0

    def get_multi_odds(self, ids, bookmakers, *, sport=""):
        self.odds_calls += 1
        return self.passes.pop(0) if self.passes else []


def _q(odds, market="h2h", outcome="Home", line=""):
    from odds_watcher.odds_api import Quote
    return Quote("e1", "bet365", market, line, outcome, odds)


EVENTS = [Event(id="e1", start_ts=9e9, home="Ajax", away="PSV")]


def test_verify_reports_a_real_price_move(config, wired, capsys):
    api = wired(TwoPassApi([[_q(2.10), _q(1.80, outcome="Away")],
                            [_q(1.95), _q(1.80, outcome="Away")]], events=EVENTS))
    rc = cli.cmd_verify(config, wait=1, sleep=lambda s: None)

    out = capsys.readouterr().out
    assert rc == 0
    assert "2 line(s) present in both passes" in out
    assert "1 of them changed price" in out
    assert "sharpest shortening in 1s: 7.14%" in out
    assert api.odds_calls == 2


def test_verify_names_unstable_line_identity(config, wired, capsys):
    """A moved handicap makes a different line, so nothing is ever compared."""
    wired(TwoPassApi([[_q(2.10, market="totals", line="2.5")],
                      [_q(2.10, market="totals", line="3.0")]], events=EVENTS))
    rc = cli.cmd_verify(config, wait=1, sleep=lambda s: None)

    out = capsys.readouterr().out
    assert rc == 1
    assert "no line survived from one pass to the next" in out
    assert "if a book moves its handicap" in out


def test_verify_distinguishes_a_quiet_market(config, wired, capsys):
    wired(TwoPassApi([[_q(2.10)], [_q(2.10)]], events=EVENTS))
    rc = cli.cmd_verify(config, wait=1, sleep=lambda s: None)

    out = capsys.readouterr().out
    assert rc == 0
    assert "prices are stable over 1s" in out
    assert "quiet market is the honest answer" in out


def test_verify_calls_out_an_empty_first_pass(config, wired, capsys):
    """No prices at all is a config fault, not a quiet market."""
    wired(TwoPassApi([[], [], []], events=EVENTS))
    rc = cli.cmd_verify(config, wait=1, sleep=lambda s: None)

    _out, err = capsys.readouterr()
    assert rc == 1
    assert "no prices came back for any of these fixtures" in err
    assert "returns an empty payload rather than an error" in err


# --- applying a preset must never destroy what is already configured -------

PRESET = """# a preset
ODDS_PROVIDER=parlay-api
SPORTS=baseball_mlb
MIN_DROP_PCT=10.0

ODDS_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
"""


def test_preset_carries_over_the_existing_secrets(tmp_path, capsys):
    """Copying a preset over a working .env is how the credentials got lost."""
    env = tmp_path / ".env"
    env.write_text("ODDS_API_KEY=secret-key\nTELEGRAM_BOT_TOKEN=tok\n"
                   "TELEGRAM_CHAT_ID=42\nSPORTS=soccer_epl\n")
    source = tmp_path / ".env.mlb.example"
    source.write_text(PRESET)

    rc = cli.cmd_preset(source, env)

    written = cli._read_env_file(env)
    assert rc == 0
    assert written["ODDS_API_KEY"] == "secret-key"
    assert written["TELEGRAM_BOT_TOKEN"] == "tok"
    assert written["TELEGRAM_CHAT_ID"] == "42"
    assert written["SPORTS"] == "baseball_mlb"      # the preset's value wins
    out = capsys.readouterr().out
    assert "carried over 3 existing value(s)" in out
    assert "secret-key" not in out                  # never printed


def test_preset_keeps_a_backup(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ODDS_API_KEY=secret-key\n")
    source = tmp_path / "p.example"
    source.write_text(PRESET)

    cli.cmd_preset(source, env)

    backup = tmp_path / ".env.bak"
    assert "ODDS_API_KEY=secret-key" in backup.read_text()
    assert oct(backup.stat().st_mode)[-3:] == "600"
    assert oct(env.stat().st_mode)[-3:] == "600"


def test_preset_does_not_silently_drop_unmentioned_settings(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("ODDS_API_KEY=k\nTELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=1\n"
                   "PARLAY_API_BASE_URL=https://example.test\n")
    source = tmp_path / "p.example"
    source.write_text(PRESET)

    cli.cmd_preset(source, env)

    written = cli._read_env_file(env)
    assert written["PARLAY_API_BASE_URL"] == "https://example.test"
    assert "does not mention" in capsys.readouterr().out


def test_preset_reports_what_is_still_missing(tmp_path, capsys):
    env = tmp_path / ".env"
    source = tmp_path / "p.example"
    source.write_text(PRESET)

    rc = cli.cmd_preset(source, env)

    assert rc == 1
    err = capsys.readouterr().err
    assert "still to fill in" in err
    assert "ODDS_API_KEY" in err


def test_preset_refuses_a_missing_source(tmp_path, capsys):
    rc = cli.cmd_preset(tmp_path / "nope.example", tmp_path / ".env")
    assert rc == 2
    assert "no such preset" in capsys.readouterr().err


class EmptyThenFull(FakeApi):
    """No prices when ids are named, prices when they are not."""

    def __init__(self, quotes, events=()):
        super().__init__(events=events)
        self.quotes = quotes

    def get_multi_odds(self, ids, bookmakers, *, sport=""):
        return [] if ids else list(self.quotes)


def test_verify_names_an_event_id_mismatch(config, wired, capsys):
    """Prices exist but under other ids: filtered out before being recorded."""
    from odds_watcher.odds_api import Quote

    wired(EmptyThenFull([Quote("odds-side-1", "bet365", "h2h", "", "Home", 2.0)],
                        events=[Event(id="events-side-1", start_ts=9e9,
                                      home="A", away="B")]))
    rc = cli.cmd_verify(config, wait=1, sleep=lambda s: None)

    err = capsys.readouterr().err
    assert rc == 1
    assert "do not match the fixture list" in err
    assert "events-side-1" in err and "odds-side-1" in err
    assert "bug in the provider client" in err


def test_verify_separates_an_empty_provider_from_a_mismatch(config, wired, capsys):
    """Nothing even without the filter is a request-shape problem."""
    wired(EmptyThenFull([], events=[Event(id="e1", start_ts=9e9, home="A", away="B")]))
    rc = cli.cmd_verify(config, wait=1, sleep=lambda s: None)

    err = capsys.readouterr().err
    assert rc == 1
    assert "even with no event filter" in err
    assert "request-shape problem" in err
    assert "do not match" not in err


def test_preset_does_not_overwrite_a_deliberately_empty_setting(tmp_path, capsys):
    """An empty setting means "off"; only an empty credential means "unfilled"."""
    env = tmp_path / ".env"
    env.write_text("ODDS_API_KEY=k\nTELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=1\n"
                   "FEATURED_MARKETS=all\n")
    source = tmp_path / "props-only.example"
    source.write_text("FEATURED_MARKETS=\nPROP_MARKETS=all\n"
                      "ODDS_API_KEY=\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n")

    cli.cmd_preset(source, env)

    written = cli._read_env_file(env)
    assert written["FEATURED_MARKETS"] == ""      # the preset turned it off
    assert written["ODDS_API_KEY"] == "k"         # the secret was carried over
