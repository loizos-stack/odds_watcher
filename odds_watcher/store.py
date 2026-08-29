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
    event_name    TEXT NOT NULL DEFAULT '',
    samples       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (event_id, bookmaker, market, line, outcome)
);

CREATE TABLE IF NOT EXISTS api_calls (
    ts   REAL NOT NULL,
    cost INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls (ts);

CREATE TABLE IF NOT EXISTS market_keys (
    sport      TEXT NOT NULL,
    key        TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    checked_at REAL NOT NULL,
    PRIMARY KEY (sport, key)
);
CREATE TABLE IF NOT EXISTS fixtures (
    id         TEXT PRIMARY KEY,
    start_ts   REAL NOT NULL,
    home       TEXT NOT NULL,
    away       TEXT NOT NULL,
    sport      TEXT NOT NULL DEFAULT '',
    sport_key  TEXT NOT NULL DEFAULT '',
    league     TEXT NOT NULL DEFAULT '',
    league_slug TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
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

        After an alert we re-baseline to the alerted price, so a line that
        keeps sliding is reported as each new step down rather than as an
        ever-growing total from where it started.
        """
        return self.alert_odds if self.alert_odds else self.baseline_odds

    @property
    def reference_ts(self) -> float:
        """When the reference price was recorded, to report alongside it."""
        return self.alert_ts if self.alert_odds else self.baseline_ts


# Bumped whenever a provider change alters what a stored fixture MEANS --
# most importantly which identifier its `id` holds. A cached list written
# under the old meaning cannot be matched against freshly parsed prices, and
# is discarded rather than silently mis-joined.
FIXTURE_CACHE_VERSION = "2"


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring an older database up to the current schema in place."""
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(api_calls)")}
        if "cost" not in columns:
            # Databases created before metered providers counted one per call.
            self.conn.execute("ALTER TABLE api_calls ADD COLUMN cost INTEGER NOT NULL DEFAULT 1")
        line_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(line_state)")}
        if "event_name" not in line_columns:
            # Recorded so a movement report can name the fixture.
            self.conn.execute(
                "ALTER TABLE line_state ADD COLUMN event_name TEXT NOT NULL DEFAULT ''"
            )
        if "samples" not in line_columns:
            # A line seen once cannot move, however far the market ran. Without
            # this count that case is indistinguishable from a quiet market.
            self.conn.execute(
                "ALTER TABLE line_state ADD COLUMN samples INTEGER NOT NULL DEFAULT 1"
            )

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
        event_name: str = "",
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
                    baseline_pre_window, last_odds, last_ts, event_start, event_name)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*quote.key, quote.odds, ts, int(pre_window), quote.odds, ts, event_start,
                 event_name),
            )
            return

        if pre_window:
            self.conn.execute(
                """UPDATE line_state
                   SET baseline_odds=?, baseline_ts=?, baseline_pre_window=1,
                       last_odds=?, last_ts=?, event_start=?, samples=samples+1
                   WHERE event_id=? AND bookmaker=? AND market=? AND line=? AND outcome=?""",
                (quote.odds, ts, quote.odds, ts, event_start, *quote.key),
            )
        else:
            self.conn.execute(
                """UPDATE line_state SET last_odds=?, last_ts=?, event_start=?, samples=samples+1
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

    def movements(self, since_ts: float = 0.0, limit: int = 40) -> list:
        """Every tracked line with how far it moved, biggest drop first.

        Reports what the prices actually did regardless of the alert
        threshold, which is the only way to tell "nothing moved" apart from
        "the threshold is too high".
        """
        rows = self.conn.execute(
            """SELECT event_name, event_id, bookmaker, market, line, outcome,
                      baseline_odds, last_odds, alert_count, event_start,
                      baseline_pre_window, samples
               FROM line_state
               WHERE event_start >= ? AND baseline_odds > 0
               ORDER BY (baseline_odds - last_odds) / baseline_odds DESC
               LIMIT ?""",
            (since_ts, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def movement_summary(self, since_ts: float = 0.0) -> dict:
        """Counts of tracked lines by how far each moved."""
        row = self.conn.execute(
            """SELECT
                 COUNT(*) AS tracked,
                 SUM(CASE WHEN last_odds < baseline_odds THEN 1 ELSE 0 END) AS fell,
                 SUM(CASE WHEN last_odds > baseline_odds THEN 1 ELSE 0 END) AS rose,
                 SUM(CASE WHEN last_odds = baseline_odds THEN 1 ELSE 0 END) AS flat,
                 MAX((baseline_odds - last_odds) / baseline_odds) AS biggest_drop,
                 SUM(baseline_pre_window) AS alertable,
                 SUM(CASE WHEN samples < 2 THEN 1 ELSE 0 END) AS seen_once,
                 SUM(CASE WHEN baseline_pre_window = 0
                          AND last_odds < baseline_odds THEN 1 ELSE 0 END) AS fell_unalertable
               FROM line_state WHERE event_start >= ? AND baseline_odds > 0""",
            (since_ts,),
        ).fetchone()
        return dict(row) if row else {}

    def purge(self, older_than_ts: float) -> int:
        """Drop state for events that kicked off long ago."""
        cursor = self.conn.execute("DELETE FROM line_state WHERE event_start < ?", (older_than_ts,))
        self.conn.commit()
        return cursor.rowcount

    def tracked_lines(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM line_state").fetchone()[0]

    # -- the fixture list --------------------------------------------------
    def save_fixtures(self, events, now: float, *, partial: bool, scope: str = "") -> None:
        """Persist the fixture list so a restart does not re-buy it.

        Listing every sport is one request each -- hundreds in a burst. Held
        only in memory, that burst is repaid on every restart, which during a
        session of configuration changes is most of an allowance.
        """
        self.conn.execute("DELETE FROM fixtures")
        self.conn.executemany(
            """INSERT INTO fixtures
               (id, start_ts, home, away, sport, sport_key, league, league_slug)
               VALUES (?,?,?,?,?,?,?,?)""",
            [(e.id, e.start_ts, e.home, e.away, e.sport, e.sport_key, e.league,
              e.league_slug) for e in events],
        )
        self.conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [("fixtures_fetched_at", repr(float(now))),
             ("fixtures_partial", "1" if partial else "0"),
             ("fixtures_version", FIXTURE_CACHE_VERSION),
             ("fixtures_scope", scope)],
        )
        self.conn.commit()

    def load_fixtures(self, scope: str = ""):
        """``(events, fetched_at, partial)`` from the last run, or ``([], 0, False)``."""
        from .odds_api import Event

        rows = self.conn.execute(
            "SELECT id, start_ts, home, away, sport, sport_key, league, league_slug "
            "FROM fixtures ORDER BY start_ts"
        ).fetchall()
        if not rows:
            return [], 0.0, False
        meta = {
            row["key"]: row["value"]
            for row in self.conn.execute("SELECT key, value FROM meta")
        }
        if meta.get("fixtures_version") != FIXTURE_CACHE_VERSION:
            # Written before the identifiers changed meaning: matching prices
            # against it would discard every one of them.
            log.warning(
                "cached fixture list was written by an older parser; "
                "re-fetching rather than matching prices against stale ids"
            )
            return [], 0.0, False
        if meta.get("fixtures_scope", "") != scope:
            # SPORTS or LEAGUES changed. Keeping this list would poll sports
            # that are no longer configured and miss ones that now are.
            log.info(
                "cached fixture list was for a different SPORTS/LEAGUES; re-fetching"
            )
            return [], 0.0, False
        try:
            fetched_at = float(meta.get("fixtures_fetched_at", "0"))
        except ValueError:
            fetched_at = 0.0
        events = [
            Event(id=row["id"], start_ts=row["start_ts"], home=row["home"],
                  away=row["away"], sport=row["sport"], sport_key=row["sport_key"],
                  league=row["league"], league_slug=row["league_slug"])
            for row in rows
        ]
        return events, fetched_at, meta.get("fixtures_partial") == "1"

    # -- learned market keys ----------------------------------------------
    def get_market_keys(self, sport: str, max_age: float, now: Optional[float] = None):
        """Cached ``(usable keys, checked_at)`` for a sport, or ``(None, None)``.

        Providers that require markets to be named by request will reject the
        whole call for one bad key, so the working set is learned once and
        reused rather than rediscovered every poll.
        """
        now = now_ts() if now is None else now
        rows = self.conn.execute(
            "SELECT key, ok, checked_at FROM market_keys WHERE sport = ?", (sport,)
        ).fetchall()
        if not rows:
            return None, None
        checked_at = max(row["checked_at"] for row in rows)
        if now - checked_at > max_age:
            return None, checked_at
        return [row["key"] for row in rows if row["ok"]], checked_at

    def save_market_keys(self, sport: str, keys: dict, now: Optional[float] = None) -> None:
        """Persist ``{key: usable}`` for a sport."""
        now = now_ts() if now is None else now
        self.conn.executemany(
            """INSERT INTO market_keys (sport, key, ok, checked_at) VALUES (?,?,?,?)
               ON CONFLICT(sport, key) DO UPDATE SET ok=excluded.ok, checked_at=excluded.checked_at""",
            [(sport, key, int(bool(ok)), now) for key, ok in keys.items()],
        )
        self.conn.commit()

    # -- request budget ---------------------------------------------------
    def count_calls_since(self, since_ts: float) -> int:
        """Units spent since `since_ts`.

        A unit is one request on a per-request provider, or one credit where
        usage is metered by markets x regions.
        """
        total = self.conn.execute(
            "SELECT SUM(cost) FROM api_calls WHERE ts >= ?", (since_ts,)
        ).fetchone()[0]
        return int(total or 0)

    def add_call(self, ts: Optional[float] = None, cost: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO api_calls (ts, cost) VALUES (?, ?)",
            (now_ts() if ts is None else ts, max(int(cost), 1)),
        )
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

    def try_consume(self, cost: int = 1) -> bool:
        """Reserve `cost` units, refusing when the allowance cannot cover them."""
        hour, day = self.remaining()
        if hour < cost or day < cost:
            log.warning(
                "request budget exhausted (need %d, have %d this hour / %d today)",
                cost,
                hour,
                day,
            )
            return False
        now = self.clock()
        self.store.add_call(now, cost=cost)
        self.store.prune_calls(now - 86400 * 2)
        return True
