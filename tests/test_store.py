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


def test_records_are_batched_not_committed_per_row(tmp_path):
    """One fsync per price would make a full-market poll take minutes.

    Covers insert *and* both update paths: a poll inside the window only ever
    takes the update branch, which is where a stray commit hides.
    """
    import sqlite3

    path = tmp_path / "batch.db"
    store = Store(path)
    other = sqlite3.connect(str(path))

    def visible_odds():
        row = other.execute("SELECT last_odds FROM line_state").fetchone()
        return row[0] if row else None

    store.record(quote(2.00), pre_window=True, event_start=1000, ts=1)
    assert visible_odds() is None  # insert not committed
    store.commit()
    assert visible_odds() == 2.00

    store.record(quote(1.90), pre_window=True, event_start=1000, ts=2)
    assert visible_odds() == 2.00  # pre-window update not committed
    store.record(quote(1.80), pre_window=False, event_start=1000, ts=3)
    assert visible_odds() == 2.00  # in-window update not committed either
    store.commit()
    assert visible_odds() == 1.80

    other.close()
    store.close()


def test_record_accepts_a_prefetched_row(tmp_path):
    """The detector already holds the row; re-reading it doubles the SELECTs."""
    store = Store(tmp_path / "prefetch.db")
    store.record(quote(2.00), pre_window=True, event_start=1000, ts=1)
    state = store.get_state(quote())

    store.record(quote(1.80), pre_window=False, event_start=1000, ts=2, existing=state)
    assert store.get_state(quote()).last_odds == 1.80
    assert store.get_state(quote()).baseline_odds == 2.00
    store.close()


def test_closing_flushes_pending_writes(tmp_path):
    path = tmp_path / "flush.db"
    with Store(path) as store:
        store.record(quote(2.20), pre_window=True, event_start=1000, ts=1)
    with Store(path) as reopened:
        assert reopened.get_state(quote()).baseline_odds == 2.20


def test_budget_accounts_for_metered_credits(store):
    """A provider charging markets x regions must not be counted as one call."""
    clock = {"now": 10_000.0}
    budget = RequestBudget(store, per_hour=10, per_day=100, clock=lambda: clock["now"])

    assert budget.try_consume(cost=3) is True
    assert budget.remaining()[0] == 7
    assert budget.try_consume(cost=6) is True
    assert budget.remaining()[0] == 1
    assert budget.try_consume(cost=3) is False  # 3 will not fit in 1
    assert budget.try_consume(cost=1) is True


def test_old_databases_gain_the_cost_column(tmp_path):
    """An existing install must keep working across the schema change."""
    import sqlite3

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(path))
    legacy.executescript(
        "CREATE TABLE api_calls (ts REAL NOT NULL);"
        "INSERT INTO api_calls (ts) VALUES (1.0), (2.0);"
    )
    legacy.commit()
    legacy.close()

    store = Store(path)
    assert store.count_calls_since(0) == 2  # pre-existing rows count as one each
    store.add_call(3.0, cost=5)
    assert store.count_calls_since(0) == 7
    store.close()


def test_market_keys_expire(store):
    store.save_market_keys("baseball_mlb", {"batter_hits": True, "batter_triples": False}, now=1000)

    fresh, checked = store.get_market_keys("baseball_mlb", max_age=3600, now=2000)
    assert fresh == ["batter_hits"]  # rejected keys are never handed back
    assert checked == 1000

    stale, checked = store.get_market_keys("baseball_mlb", max_age=100, now=2000)
    assert stale is None  # expired, so the caller re-probes
    assert checked == 1000

    assert store.get_market_keys("basketball_nba", max_age=3600, now=2000) == (None, None)


def test_market_keys_update_in_place(store):
    store.save_market_keys("baseball_mlb", {"batter_hits": False}, now=1000)
    assert store.get_market_keys("baseball_mlb", max_age=3600, now=1001)[0] == []
    store.save_market_keys("baseball_mlb", {"batter_hits": True}, now=1002)
    assert store.get_market_keys("baseball_mlb", max_age=3600, now=1003)[0] == ["batter_hits"]
