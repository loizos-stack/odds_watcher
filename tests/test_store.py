"""Persistence: baselines survive restarts, budget is enforced across runs."""

from odds_watcher.odds_api import Quote
from odds_watcher.store import RequestBudget, Store


def quote(odds=2.0):
    return Quote("e1", "bet365", "moneyline", "", "Home", odds)


def test_baseline_follows_price_until_the_window_opens(store):
    store.record(quote(2.50), pre_window=True, event_start=1000, ts=1)
    store.record(quote(2.20), pre_window=True, event_start=1000, ts=2)
    state = store.get_state(quote())
    assert state.baseline_odds == 2.20
    assert state.baseline_pre_window is True


def test_baseline_freezes_inside_the_window(store):
    store.record(quote(2.20), pre_window=True, event_start=1000, ts=1)
    store.record(quote(1.90), pre_window=False, event_start=1000, ts=2)
    state = store.get_state(quote())
    assert state.baseline_odds == 2.20
    assert state.last_odds == 1.90


def test_state_survives_reopening_the_database(tmp_path):
    path = tmp_path / "state.db"
    with Store(path) as first:
        first.record(quote(2.20), pre_window=True, event_start=1000, ts=1)
    with Store(path) as second:
        assert second.get_state(quote()).baseline_odds == 2.20


def test_mark_alerted_moves_the_reference(store):
    store.record(quote(2.00), pre_window=True, event_start=1000, ts=1)
    assert store.get_state(quote()).reference_odds == 2.00
    store.mark_alerted(quote(1.80), ts=2)
    state = store.get_state(quote())
    assert state.reference_odds == 1.80
    assert state.alert_count == 1


def test_purge_removes_finished_events(store):
    store.record(quote(2.00), pre_window=True, event_start=1000, ts=1)
    assert store.tracked_lines() == 1
    assert store.purge(older_than_ts=5000) == 1
    assert store.tracked_lines() == 0


def test_budget_is_rolling(store):
    clock = {"now": 10_000.0}
    budget = RequestBudget(store, per_hour=3, per_day=5, clock=lambda: clock["now"])

    assert [budget.try_consume() for _ in range(4)] == [True, True, True, False]
    assert budget.remaining() == (0, 2)

    clock["now"] += 3601  # the hour rolls over
    assert budget.try_consume() is True
    assert budget.remaining()[0] == 2


def test_budget_daily_cap(store):
    clock = {"now": 10_000.0}
    budget = RequestBudget(store, per_hour=100, per_day=2, clock=lambda: clock["now"])
    assert budget.try_consume() and budget.try_consume()
    clock["now"] += 3601
    assert budget.try_consume() is False
    clock["now"] += 86_401
    assert budget.try_consume() is True
