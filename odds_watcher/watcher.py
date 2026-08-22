"""The polling loop that ties the API, the detector and Telegram together.

Request budget is the scarce resource on the free tier (100 requests/hour,
500/day), so the loop is deliberately frugal:

* the fixture list is refreshed at most every ``EVENTS_REFRESH_SECONDS``;
* odds are only requested for events that are already inside the tracking lead
  (``BASELINE_LEAD_SECONDS`` before kick-off), batched through ``/odds/multi``;
* when nothing is close to kick-off the loop sleeps until the next fixture
  needs attention instead of polling on a fixed tick.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Iterable, Optional, Sequence

from .config import Config
from .detector import Alert, DropDetector
from .http import HttpError
from .odds_api import BudgetExceeded, Event, OddsApiClient, Quote
from .store import Store
from .telegram import TelegramClient, format_digest
from .util import format_countdown, now_ts

log = logging.getLogger(__name__)

EVENTS_PER_ODDS_REQUEST = 20
ALERTS_PER_MESSAGE = 5
STATE_RETENTION_SECONDS = 6 * 3600


def chunked(items: Sequence, size: int) -> Iterable[Sequence]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class Watcher:
    def __init__(
        self,
        config: Config,
        api: OddsApiClient,
        telegram: TelegramClient,
        store: Store,
        clock=now_ts,
        sleep=time.sleep,
    ):
        self.config = config
        self.api = api
        self.telegram = telegram
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self.detector = DropDetector(config, store)
        self._events: list[Event] = []
        self._events_fetched_at: float = 0.0

    # -- fixtures ---------------------------------------------------------
    def refresh_events(self, now: float, *, force: bool = False) -> list[Event]:
        """Re-fetch the fixture list when the cache has gone stale."""
        if not force and self._events and now - self._events_fetched_at < self.config.events_refresh_seconds:
            return self._events

        events: dict[str, Event] = {}
        targets = [(sport, league) for sport in self.config.sports for league in (self.config.leagues or [None])]
        for sport, league in targets:
            try:
                for event in self.api.get_events(sport, league=league):
                    events[event.id] = event
            except BudgetExceeded as exc:
                log.warning("%s", exc)
                break
            except HttpError as exc:
                log.error("failed to load events for sport=%s league=%s: %s", sport, league, exc)

        if events or force:
            self._events = sorted(events.values(), key=lambda e: e.start_ts)
            self._events_fetched_at = now
            log.info("fixture list refreshed: %d event(s)", len(self._events))
        return self._events

    def events_in_tracking_range(self, now: float) -> list[Event]:
        """Fixtures close enough to kick-off that their prices matter."""
        cfg = self.config
        return [
            event
            for event in self._events
            if cfg.window_end_seconds <= event.seconds_to_start(now) <= cfg.baseline_lead_seconds
        ]

    # -- one poll ---------------------------------------------------------
    def poll_once(self, now: Optional[float] = None) -> list[Alert]:
        now = self.clock() if now is None else now
        self.refresh_events(now)
        events = self.events_in_tracking_range(now)
        if not events:
            log.debug("nothing within tracking range")
            return []

        by_id = {event.id: event for event in events}
        quotes_by_event: dict[str, list[Quote]] = defaultdict(list)
        for batch in chunked([event.id for event in events], EVENTS_PER_ODDS_REQUEST):
            try:
                for quote in self.api.get_multi_odds(batch, self.config.bookmakers):
                    quotes_by_event[quote.event_id].append(quote)
            except BudgetExceeded as exc:
                log.warning("%s", exc)
                break
            except HttpError as exc:
                log.error("odds request failed: %s", exc)

        alerts: list[Alert] = []
        for event_id, quotes in quotes_by_event.items():
            event = by_id.get(event_id)
            if event is None:
                continue
            alerts.extend(self.detector.process(event, quotes, now))

        if alerts:
            self.dispatch(alerts, now)

        self.store.purge(now - STATE_RETENTION_SECONDS)
        return alerts

    def dispatch(self, alerts: list[Alert], now: float) -> None:
        """Send alerts to Telegram, marking each one only once it is delivered."""
        for group in chunked(alerts, ALERTS_PER_MESSAGE):
            try:
                self.telegram.send_message(format_digest(list(group)))
            except HttpError:
                log.error("could not deliver %d alert(s); will retry next poll", len(group))
                continue
            for alert in group:
                self.store.mark_alerted(alert.quote, ts=now)

    # -- loop -------------------------------------------------------------
    def seconds_until_next_poll(self, now: float) -> int:
        """Poll fast when a fixture is live in the window, idle otherwise."""
        cfg = self.config
        if self.events_in_tracking_range(now):
            return cfg.poll_interval_seconds

        upcoming = [
            event.seconds_to_start(now) - cfg.baseline_lead_seconds
            for event in self._events
            if event.seconds_to_start(now) > cfg.baseline_lead_seconds
        ]
        wait = min(upcoming) if upcoming else cfg.idle_poll_interval_seconds
        return int(max(cfg.poll_interval_seconds, min(wait, cfg.idle_poll_interval_seconds)))

    def run_forever(self) -> None:
        cfg = self.config
        log.info(
            "watching %s on %s | alert when a price drops >= %.1f%% %s",
            ", ".join(cfg.sports),
            ", ".join(cfg.bookmakers),
            cfg.min_drop_pct,
            cfg.alert_window_label,
        )
        while True:
            started = self.clock()
            try:
                self.poll_once(started)
            except Exception:  # keep the daemon alive across transient faults
                log.exception("poll failed")
            delay = self.seconds_until_next_poll(self.clock())
            hour, day = getattr(self.api.budget, "remaining", lambda: (-1, -1))()
            log.debug(
                "sleeping %s (budget left: %s/hour, %s/day, %d lines tracked)",
                format_countdown(delay),
                hour,
                day,
                self.store.tracked_lines(),
            )
            self.sleep(delay)
