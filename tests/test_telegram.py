from odds_watcher.detector import Alert
from odds_watcher.odds_api import Event, Quote
from odds_watcher.telegram import TelegramClient, format_alert, format_digest

EVENT = Event(
    id="e1",
    start_ts=1_700_000_000.0,
    home="Ajax <A>",
    away="PSV",
    sport="football",
    league="Eredivisie",
)


def alert(**overrides):
    defaults = dict(
        event=EVENT,
        quote=Quote("e1", "bet365", "moneyline", "", "Home", 1.80),
        reference_odds=2.00,
        drop_pct=10.0,
        seconds_to_start=304,
    )
    defaults.update(overrides)
    return Alert(**defaults)


def test_format_alert_contains_the_essentials():
    text = format_alert(alert())
    assert "BET365" in text
    assert "Ajax &lt;A&gt; vs PSV" in text  # user data is HTML-escaped
    assert "5m 04s" in text
    assert "2.00" in text and "1.80" in text and "-10.0%" in text


def test_repeat_alerts_are_labelled():
    assert "continuing" in format_alert(alert(repeat=True))


def test_digest_joins_multiple_alerts():
    text = format_digest([alert(), alert(quote=Quote("e1", "betano", "moneyline", "", "Away", 3.1))])
    assert text.count("Odds drop") == 2
    assert "BETANO" in text


def test_dry_run_does_not_send(caplog):
    client = TelegramClient("token", "42", dry_run=True)
    assert client.send_message("hello") is None


def test_url_building():
    client = TelegramClient("abc", "42")
    assert client._url("sendMessage") == "https://api.telegram.org/botabc/sendMessage"


def test_chat_id_reports_a_telegram_outage_without_a_traceback(monkeypatch, capsys, tmp_path):
    from odds_watcher.cli import cmd_chat_id
    from odds_watcher.config import Config
    from odds_watcher.http import TransportError

    def explode(self):
        raise TransportError("api.telegram.org unreachable after 3 attempts")

    monkeypatch.setattr(TelegramClient, "get_updates", explode)
    config = Config.from_env(
        {"TELEGRAM_BOT_TOKEN": "t", "DB_PATH": str(tmp_path / "s.db")},
        required=("TELEGRAM_BOT_TOKEN",),
    )
    assert cmd_chat_id(config) == 1
    assert "could not reach Telegram" in capsys.readouterr().err
