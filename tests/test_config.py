import pytest

from odds_watcher.config import Config, ConfigError, load_dotenv

BASE = {"ODDS_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}


def test_defaults_match_the_stated_rule():
    config = Config.from_env(BASE)
    assert config.bookmakers == ("bet365", "betano")
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
