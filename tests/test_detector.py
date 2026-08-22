"""Behaviour of the drop rule: only in-window drops, measured pre-window."""

import pytest

from odds_watcher.detector import DropDetector, drop_pct, market_allowed
from odds_watcher.odds_api import Event, Quote

KICKOFF = 1_700_000_000.0
EVENT = Event(id="e1", start_ts=KICKOFF, home="Ajax", away="PSV", sport="football", league="Eredivisie")


def quote(odds, bookmaker="bet365", outcome="Home", market="moneyline", line=""):
    return Quote("e1", bookmaker, market, line, outcome, odds)


def at(minutes_before):
    return KICKOFF - minutes_before * 60


def test_drop_pct():
    assert drop_pct(2.0, 1.8) == pytest.approx(10.0)
    assert drop_pct(2.0, 2.2) == pytest.approx(-10.0)
    assert drop_pct(0, 1.5) == 0.0


def test_alerts_on_drop_inside_window(config, store):
    detector = DropDetector(config, store)
    # Baseline recorded 20 minutes out, no alert possible yet.
    assert detector.process(EVENT, [quote(2.00)], at(20)) == []
    # 5 minutes out the price has shortened by 10%.
    alerts = detector.process(EVENT, [quote(1.80)], at(5))
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.reference_odds == 2.00
    assert alert.quote.odds == 1.80
    assert alert.drop_pct == pytest.approx(10.0)
    assert alert.seconds_to_start == pytest.approx(300)
    assert alert.repeat is False


def test_no_alert_below_threshold(config, store):
    detector = DropDetector(config, store)
    detector.process(EVENT, [quote(2.00)], at(20))
    assert detector.process(EVENT, [quote(1.95)], at(5)) == []  # -2.5%


def test_no_alert_when_odds_drift_out(config, store):
    detector = DropDetector(config, store)
    detector.process(EVENT, [quote(2.00)], at(20))
    assert detector.process(EVENT, [quote(2.40)], at(4)) == []


def test_drop_outside_the_window_is_ignored(config, store):
    """A 20% slide that happens an hour out must not alert."""
    detector = DropDetector(config, store)
    detector.process(EVENT, [quote(2.00)], at(40))
    assert detector.process(EVENT, [quote(1.60)], at(30)) == []
    # The baseline followed the drop, so entering the window is quiet.
    assert detector.process(EVENT, [quote(1.60)], at(8)) == []


def test_baseline_is_the_price_at_window_entry(config, store):
    detector = DropDetector(config, store)
    detector.process(EVENT, [quote(2.50)], at(40))
    detector.process(EVENT, [quote(2.00)], at(11))  # last pre-window price
    alerts = detector.process(EVENT, [quote(1.90)], at(9))
    assert alerts[0].reference_odds == 2.00
    assert alerts[0].drop_pct == pytest.approx(5.0)


def test_event_first_seen_inside_window_never_alerts(config, store):
    """Without a pre-window reference there is nothing to compare against."""
    detector = DropDetector(config, store)
    detector.process(EVENT, [quote(2.00)], at(9))
    assert detector.process(EVENT, [quote(1.50)], at(3)) == []


def test_repeat_alert_requires_another_full_threshold(config, store):
    detector = DropDetector(config, store)
    detector.process(EVENT, [quote(2.00)], at(20))
    first = detector.process(EVENT, [quote(1.80)], at(8))
    store.mark_alerted(first[0].quote, ts=at(8))

    # A further 2% slide is not enough for a second message.
    assert detector.process(EVENT, [quote(1.76)], at(6)) == []

    second = detector.process(EVENT, [quote(1.70)], at(4))
    assert len(second) == 1
    assert second[0].reference_odds == 1.80
    assert second[0].repeat is True


def test_after_kickoff_is_ignored(config, store):
    detector = DropDetector(config, store)
    detector.process(EVENT, [quote(2.00)], at(20))
    assert detector.process(EVENT, [quote(1.00)], at(-1)) == []


def test_other_bookmakers_are_dropped(config, store):
    detector = DropDetector(config, store)
    detector.process(EVENT, [quote(2.00, bookmaker="pinnacle")], at(20))
    assert detector.process(EVENT, [quote(1.50, bookmaker="pinnacle")], at(5)) == []


def test_both_configured_books_are_tracked_independently(config, store):
    detector = DropDetector(config, store)
    detector.process(EVENT, [quote(2.00, bookmaker="bet365"), quote(2.10, bookmaker="betano")], at(20))
    alerts = detector.process(
        EVENT, [quote(1.80, bookmaker="bet365"), quote(1.85, bookmaker="betano")], at(5)
    )
    assert {a.quote.bookmaker for a in alerts} == {"bet365", "betano"}


def test_very_short_prices_are_ignored(config, store):
    detector = DropDetector(config, store)
    detector.process(EVENT, [quote(1.04)], at(20))
    assert detector.process(EVENT, [quote(1.01)], at(5)) == []


def test_market_filter():
    assert market_allowed("moneyline", ())
    assert market_allowed("moneyline", ("moneyline",))
    assert market_allowed("Asian Handicap", ("handicap",))
    assert not market_allowed("totals", ("moneyline",))
