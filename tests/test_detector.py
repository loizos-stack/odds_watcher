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


def test_market_exclusions():
    """`-` entries exclude, so full-time totals can be kept without the HT ones."""
    wanted = ("Totals", "Corner", "Booking", "-HT")
    assert market_allowed("Totals", wanted)
    assert market_allowed("Corner Totals", wanted)
    assert market_allowed("Bookings Over/Under", wanted)
    assert not market_allowed("Totals HT", wanted)
    assert not market_allowed("ML", wanted)


def test_exclusions_only_means_everything_else():
    assert market_allowed("Totals", ("-HT",))
    assert not market_allowed("Totals HT", ("-HT",))


def test_outcome_filter_keeps_over_under_only():
    from odds_watcher.detector import outcome_allowed

    assert outcome_allowed("over", ("over", "under"))
    assert outcome_allowed("Under", ("over", "under"))
    assert not outcome_allowed("home", ("over", "under"))
    assert outcome_allowed("home", ())  # unset means no restriction


def test_totals_config_ignores_the_handicap_side_of_a_matching_market(config, store):
    """A market named ...Totals may still carry home/away rows; OUTCOMES filters them."""
    import dataclasses

    cfg = dataclasses.replace(config, markets=("Totals",), outcomes=("over", "under"))
    detector = DropDetector(cfg, store)

    over = Quote("e1", "bet365", "Corner Totals", "9.5", "over", 2.00)
    home = Quote("e1", "bet365", "Corner Totals", "9.5", "home", 2.00)
    detector.process(EVENT, [over, home], at(20))

    alerts = detector.process(
        EVENT,
        [
            Quote("e1", "bet365", "Corner Totals", "9.5", "over", 1.75),
            Quote("e1", "bet365", "Corner Totals", "9.5", "home", 1.75),
        ],
        at(5),
    )
    assert [a.quote.outcome for a in alerts] == ["over"]


def test_totals_lines_are_tracked_separately(config, store):
    """Over 2.5 and Over 3.5 are different bets and must not share a baseline."""
    import dataclasses

    cfg = dataclasses.replace(config, markets=("Totals",), outcomes=("over", "under"))
    detector = DropDetector(cfg, store)

    detector.process(
        EVENT,
        [
            Quote("e1", "bet365", "Totals", "2.5", "over", 1.90),
            Quote("e1", "bet365", "Totals", "3.5", "over", 3.20),
        ],
        at(20),
    )
    alerts = detector.process(
        EVENT,
        [
            Quote("e1", "bet365", "Totals", "2.5", "over", 1.90),
            Quote("e1", "bet365", "Totals", "3.5", "over", 2.80),
        ],
        at(5),
    )
    assert len(alerts) == 1
    assert alerts[0].quote.line == "3.5"
    assert alerts[0].reference_odds == 3.20


def test_bookmaker_matching_is_case_insensitive(config, store):
    """Config says "DraftKings"; the payload key is whatever the API sends."""
    import dataclasses

    cfg = dataclasses.replace(config, bookmakers=("Bet365", "DraftKings"))
    detector = DropDetector(cfg, store)

    for name in ("draftkings", "DraftKings", "DRAFTKINGS"):
        assert detector.relevant(quote(2.0, bookmaker=name.lower()))
    assert not detector.relevant(quote(2.0, bookmaker="betano"))


def test_switching_books_leaves_the_old_ones_inert(config, store):
    """Stale baselines from a previous bookmaker must not produce alerts."""
    import dataclasses

    old = DropDetector(dataclasses.replace(config, bookmakers=("Bet365", "Betano")), store)
    old.process(EVENT, [quote(2.00, bookmaker="betano")], at(20))

    new = DropDetector(dataclasses.replace(config, bookmakers=("Bet365", "DraftKings")), store)
    assert new.process(EVENT, [quote(1.50, bookmaker="betano")], at(5)) == []


def _first_seen(config, **overrides):
    import dataclasses

    return dataclasses.replace(
        config, baseline_mode="first-seen", baseline_lead_seconds=1200, **overrides
    )


def test_first_seen_mode_signals_every_successive_drop(config, store):
    """Three cascading 5%+ drops from when tracking starts are three signals."""
    detector = DropDetector(_first_seen(config), store)
    signals = []

    for minutes, price in [(20, 2.00), (17, 1.89), (14, 1.79), (11, 1.69)]:
        alerts = detector.process(EVENT, [quote(price)], at(minutes))
        for alert in alerts:
            signals.append((round(alert.reference_odds, 2), alert.quote.odds))
            store.mark_alerted(alert.quote, ts=at(minutes))

    assert signals == [(2.00, 1.89), (1.89, 1.79), (1.79, 1.69)]


def test_first_seen_mode_uses_the_very_first_price(config, store):
    """No pre-window sample is needed: the first price seen is the reference."""
    detector = DropDetector(_first_seen(config), store)
    detector.process(EVENT, [quote(2.00)], at(20))       # first sighting
    alerts = detector.process(EVENT, [quote(1.80)], at(18))
    assert alerts[0].reference_odds == 2.00
    assert alerts[0].drop_pct == pytest.approx(10.0)


def test_first_seen_baseline_does_not_follow_the_price(config, store):
    """A drifting price must not quietly reset the reference."""
    detector = DropDetector(_first_seen(config), store)
    detector.process(EVENT, [quote(2.00)], at(20))
    detector.process(EVENT, [quote(1.98)], at(19))  # small move, no alert
    detector.process(EVENT, [quote(1.96)], at(18))
    alerts = detector.process(EVENT, [quote(1.89)], at(17))
    assert alerts[0].reference_odds == 2.00  # still the first price, not 1.96


def test_first_seen_mode_stops_at_kickoff(config, store):
    detector = DropDetector(_first_seen(config), store)
    detector.process(EVENT, [quote(2.00)], at(20))
    assert detector.process(EVENT, [quote(1.50)], at(-1)) == []


def test_window_entry_mode_is_unchanged(config, store):
    """The default rule still ignores drops that finish before the window."""
    detector = DropDetector(config, store)
    detector.process(EVENT, [quote(2.00)], at(20))
    detector.process(EVENT, [quote(1.89)], at(17))
    detector.process(EVENT, [quote(1.79)], at(14))
    assert detector.process(EVENT, [quote(1.69)], at(11)) == []
