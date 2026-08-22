"""SQLite persistence: price baselines, sent alerts and the request budget.

State lives on disk so that a restart (or a cron-driven ``--once`` run) keeps
its baselines, does not re-send an alert it already sent, and does not blow the
free tier's hourly/daily request allowance.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from .odds_api import Quote
from .util import now_ts

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS line_state (
    event_id      TEXT NOT NULL,
    bookmaker     TEXT NOT NULL,
    market        TEXT NOT NULL,
    line          TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    baseline_odds REAL NOT NULL,
    baseline_ts   REAL NOT NULL,
    baseline_pre_window INTEGER NOT NULL DEFAULT 0,
    last_odds     REAL NOT NULL,
    last_ts       REAL NOT NULL,
    alert_odds    REAL,
    alert_ts      REAL,
    alert_count   INTEGER NOT NULL DEFAULT 0,
    event_start   REAL NOT NULL,
    PRIMARY KEY (event_id, bookmaker, market, line, outcome)
);

CREATE TABLE IF NOT EXISTS api_calls (
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls (ts);
CREATE INDEX IF NOT EXISTS idx_line_state_start ON line_state (event_start);
"""


class LineState:
    """A row of ``line_state`` with the fields the detector cares about."""

    __slots__ = (
        "baseline_odds",
        "baseline_ts",
        "baseline_pre_window",
        "last_odds",
        "last_ts",
        "alert_odds",
        "alert_ts",
        "alert_count",
    )

    def __init__(self, row: sqlite3.Row):
        self.baseline_odds = row["baseline_odds"]
        self.baseline_ts = row["baseline_ts"]
        self.baseline_pre_window = bool(row["baseline_pre_window"])
        self.last_odds = row["last_odds"]
        self.last_ts = row["last_ts"]
        self.alert_odds = row["alert_odds"]
        self.alert_ts = row["alert_ts"]
        self.alert_count = row["alert_count"]

    @property
    def reference_odds(self) -> float:
        """Price a new drop is measured against.

        After an alert we re-baseline to the alerted price, so a line that keeps
        sliding produces a second alert only once it drops another full
        threshold rather than on every poll.
        """
        return self.alert_odds if self.alert_odds else self.baseline_odds


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def commit(self) -> None:
        """Flush pending writes.

        Recording a price does not commit on its own: with every market and
        player prop enabled a single poll writes thousands of rows, and one
        fsync per row makes a poll take minutes. Callers commit once when the
        poll is done instead.
        """
        self.conn.commit()

    def close(self) -> None:
        """Flush and close. Closing must never silently drop pending writes."""
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- line state -------------------------------------------------------
    def get_state(self, quote: Quote) -> Optional[LineState]:
        row = self.conn.execute(
            """SELECT * FROM line_state
               WHERE event_id=? AND bookmaker=? AND market=? AND line=? AND outcome=?""",
            quote.key,
        ).fetchone()
        return LineState(row) if row else None

    def record(
        self,
        quote: Quote,
        *,
        pre_window: bool,
        event_start: float,
        ts: Optional[float] = None,
        existing: Optional[LineState] = None,
    ) -> None:
        """Insert or update the stored price for a quote.

        While the event is still outside the alert window the baseline tracks
        the newest price, so the baseline ends up being the last price seen
        *before* the window opened. Once inside the window the baseline is
        frozen and only ``last_odds`` moves.

        Callers that already looked the row up pass it as ``existing`` to skip
        a second SELECT — worth it when a poll handles tens of thousands of
        prices. Writes are not committed here; call :meth:`commit`.
        """
        ts = now_ts() if ts is None else ts
        if existing is None:
            existing = self.get_state(quote)
        if existing is None:
            self.conn.execute(
                """INSERT INTO line_state
                   (event_id, bookmaker, market, line, outcome, baseline_odds, baseline_ts,
                    baseline_pre_window, last_odds, last_ts, event_start)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (*quote.key, quote.odds, ts, int(pre_window), quote.odds, ts, event_start),
            )
            return

        if pre_window:
            self.conn.execute(
                """UPDATE line_state
                   SET baseline_odds=?, baseline_ts=?, baseline_pre_window=1,
                       last_odds=?, last_ts=?, event_start=?
                   WHERE event_id=? AND bookmaker=? AND market=? AND line=? AND outcome=?""",
                (quote.odds, ts, quote.odds, ts, event_start, *quote.key),
            )
        else:
            self.conn.execute(
                """UPDATE line_state SET last_odds=?, last_ts=?, event_start=?
                   WHERE event_id=? AND bookmaker=? AND market=? AND line=? AND outcome=?""",
                (quote.odds, ts, event_start, *quote.key),
            )

    def mark_alerted(self, quote: Quote, ts: Optional[float] = None) -> None:
        ts = now_ts() if ts is None else ts
        self.conn.execute(
            """UPDATE line_state SET alert_odds=?, alert_ts=?, alert_count=alert_count+1
               WHERE event_id=? AND bookmaker=? AND market=? AND line=? AND outcome=?""",
            (quote.odds, ts, *quote.key),
        )
        self.conn.commit()

    def purge(self, older_than_ts: float) -> int:
        """Drop state for events that kicked off long ago."""
        cursor = self.conn.execute("DELETE FROM line_state WHERE event_start < ?", (older_than_ts,))
        self.conn.commit()
        return cursor.rowcount

    def tracked_lines(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM line_state").fetchone()[0]

    # -- request budget ---------------------------------------------------
    def count_calls_since(self, since_ts: float) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM api_calls WHERE ts >= ?", (since_ts,)
        ).fetchone()[0]

    def add_call(self, ts: Optional[float] = None) -> None:
        self.conn.execute("INSERT INTO api_calls (ts) VALUES (?)", (now_ts() if ts is None else ts,))
        self.conn.commit()

    def prune_calls(self, older_than_ts: float) -> None:
        self.conn.execute("DELETE FROM api_calls WHERE ts < ?", (older_than_ts,))
        self.conn.commit()


class RequestBudget:
    """Rolling hourly/daily cap on API calls, persisted through the store.

    The free tier allows 100 requests/hour and 500/day; the defaults leave a
    little headroom so a manual CLI call never trips the real limit.
    """

    def __init__(self, store: Store, per_hour: int, per_day: int, clock=now_ts):
        self.store = store
        self.per_hour = per_hour
        self.per_day = per_day
        self.clock = clock

    def remaining(self) -> tuple[int, int]:
        now = self.clock()
        hour = self.per_hour - self.store.count_calls_since(now - 3600)
        day = self.per_day - self.store.count_calls_since(now - 86400)
        return max(hour, 0), max(day, 0)

    def try_consume(self) -> bool:
        hour, day = self.remaining()
        if hour <= 0 or day <= 0:
            log.warning("request budget exhausted (hour=%d, day=%d remaining)", hour, day)
            return False
        now = self.clock()
        self.store.add_call(now)
        self.store.prune_calls(now - 86400 * 2)
        return True
