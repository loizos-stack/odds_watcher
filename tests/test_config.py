import pytest

from odds_watcher.config import Config, ConfigError, load_dotenv

BASE = {"ODDS_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}


def test_defaults_match_the_stated_rule():
    config = Config.from_env(BASE)
    assert config.bookmakers == ("Bet365", "DraftKings")
    assert config.sports == ("baseball",)
    assert (config.window_end_seconds, config.window_start_seconds) == (0, 600)
    assert config.alert_window_label == "0-10 min before kick-off"


def test_missing_credentials_are_reported_together():
    with pytest.raises(ConfigError) as exc:
        Config.from_env({"ODDS_API_KEY": "k"})
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value) and "TELEGRAM_CHAT_ID" in str(exc.value)


def test_window_bounds_are_validated():
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "WINDOW_START_SECONDS": "300", "WINDOW_END_SECONDS": "600"})


def test_baseline_lead_must_cover_the_window():
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "WINDOW_START_SECONDS": "600", "BASELINE_LEAD_SECONDS": "300"})


def test_numeric_validation():
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "MIN_DROP_PCT": "abc"})
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "POLL_INTERVAL_SECONDS": "1"})


def test_lists_and_flags_are_parsed():
    config = Config.from_env(
        {**BASE, "BOOKMAKERS": "bet365, betano", "SPORTS": "football,tennis", "DRY_RUN": "true"}
    )
    assert config.bookmakers == ("bet365", "betano")
    assert config.sports == ("football", "tennis")
    assert config.dry_run is True


def test_dotenv_does_not_override_real_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('ODDS_API_KEY="from-file"\nMIN_DROP_PCT=7.5\n# comment\n\n')
    monkeypatch.setenv("ODDS_API_KEY", "from-shell")
    load_dotenv(env_file)
    import os

    assert os.environ["ODDS_API_KEY"] == "from-shell"
    assert os.environ["MIN_DROP_PCT"] == "7.5"


def test_force_utf8_output_survives_odd_streams(monkeypatch):
    """Windows redirects stdout to a cp1252 stream; reconfiguring must not crash."""
    import sys

    from odds_watcher.cli import force_utf8_output

    calls = []

    class Reconfigurable:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    class Stubborn:
        def reconfigure(self, **kwargs):
            raise ValueError("underlying stream detached")

    class Ancient:  # no reconfigure at all (e.g. a captured StringIO shim)
        pass

    monkeypatch.setattr(sys, "stdout", Reconfigurable())
    monkeypatch.setattr(sys, "stderr", Stubborn())
    force_utf8_output()
    assert calls == [{"encoding": "utf-8", "errors": "replace"}]

    monkeypatch.setattr(sys, "stdout", Ancient())
    force_utf8_output()  # must not raise


def test_chat_id_command_does_not_require_the_chat_id():
    """The command that discovers the chat id must run without one."""
    config = Config.from_env({"TELEGRAM_BOT_TOKEN": "t"}, required=("TELEGRAM_BOT_TOKEN",))
    assert config.telegram_bot_token == "t"
    assert config.telegram_chat_id == ""


def test_missing_chat_id_alone_points_at_the_chat_id_command():
    with pytest.raises(ConfigError) as exc:
        Config.from_env({"ODDS_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "t"})
    assert "chat-id" in str(exc.value)


def test_status_needs_no_credentials_at_all():
    assert Config.from_env({}, required=()).bookmakers == ("Bet365", "DraftKings")


def test_bookmakers_command_needs_only_the_api_key():
    from odds_watcher.cli import REQUIRED_CREDENTIALS

    assert REQUIRED_CREDENTIALS["bookmakers"] == ("ODDS_API_KEY",)
    assert Config.from_env({"ODDS_API_KEY": "k"}, required=("ODDS_API_KEY",)) is not None


def test_baseline_lead_must_clear_the_window_by_two_polls():
    """A lead that barely clears the window lets fixtures skip the baseline."""
    with pytest.raises(ConfigError) as exc:
        Config.from_env({**BASE, "WINDOW_START_SECONDS": "600", "BASELINE_LEAD_SECONDS": "660",
                         "POLL_INTERVAL_SECONDS": "60"})
    assert "never alert" in str(exc.value)

    ok = Config.from_env({**BASE, "WINDOW_START_SECONDS": "600", "BASELINE_LEAD_SECONDS": "720",
                          "POLL_INTERVAL_SECONDS": "60"})
    assert ok.baseline_lead_seconds == 720


def test_default_lead_is_frugal_but_valid():
    config = Config.from_env(BASE)
    assert config.baseline_lead_seconds == 900
    assert config.baseline_lead_seconds >= config.window_start_seconds + 2 * config.poll_interval_seconds


def test_sport_override_does_not_need_a_correct_env(monkeypatch, tmp_path):
    """--sport exists so discovery works before SPORTS is known."""
    from odds_watcher import cli

    seen = {}

    def fake_sports(config, search=None):
        seen["sports"] = config.sports
        return 0

    monkeypatch.setattr(cli, "cmd_leagues", lambda config, search=None: fake_sports(config, search))
    monkeypatch.setenv("ODDS_API_KEY", "k")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("SPORTS", "football")

    cli.main(["leagues", "--sport", "baseball", "--env-file", str(tmp_path / "none.env")])
    assert seen["sports"] == ("baseball",)


def test_provider_selection():
    from odds_watcher.providers import build_client
    from odds_watcher.odds_api import OddsApiClient
    from odds_watcher.theoddsapi import TheOddsApiClient

    default = Config.from_env(BASE)
    assert default.odds_provider == "odds-api-io"
    assert isinstance(build_client(default), OddsApiClient)

    switched = Config.from_env({**BASE, "ODDS_PROVIDER": "the-odds-api", "SPORTS": "baseball_mlb",
                                "REGIONS": "us,uk", "PROP_MARKETS": "batter_home_runs"})
    client = build_client(switched)
    assert isinstance(client, TheOddsApiClient)
    assert client.regions == ("us", "uk")
    assert client.prop_markets == ("batter_home_runs",)
    assert client.default_sport == "baseball_mlb"


def test_unknown_provider_is_rejected():
    with pytest.raises(ConfigError) as exc:
        Config.from_env({**BASE, "ODDS_PROVIDER": "oddspapi"})
    assert "the-odds-api" in str(exc.value)


def test_status_names_settings_missing_from_the_users_env(tmp_path, capsys):
    """.env is gitignored, so new settings never arrive by pulling."""
    from odds_watcher.cli import _report_missing_settings

    (tmp_path / ".env.example").write_text(
        "# comment\nODDS_PROVIDER=odds-api-io\nREGIONS=us\nMIN_DROP_PCT=5.0\n\n"
    )
    (tmp_path / ".env").write_text("MIN_DROP_PCT=3.0\nODDS_API_KEY=secret\n")

    _report_missing_settings(tmp_path / ".env")
    out = capsys.readouterr().out
    assert "ODDS_PROVIDER" in out
    assert "REGIONS" in out
    assert "MIN_DROP_PCT" not in out  # present, so not reported


def test_no_complaint_when_the_env_is_current(tmp_path, capsys):
    from odds_watcher.cli import _report_missing_settings

    (tmp_path / ".env.example").write_text("ODDS_PROVIDER=odds-api-io\n")
    (tmp_path / ".env").write_text("ODDS_PROVIDER=the-odds-api\n")
    _report_missing_settings(tmp_path / ".env")
    assert capsys.readouterr().out == ""
