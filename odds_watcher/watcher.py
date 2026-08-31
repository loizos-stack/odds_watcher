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

import dataclasses
import logging
import time
from collections import defaultdict
from typing import Iterable, Optional, Sequence

from .config import Config
from .detector import Alert, DropDetector
from .http import TransportError
from .odds_api import BudgetExceeded, Event, Quote
from .store import Store
from .telegram import TelegramClient, format_alert, format_player_digest, split_message
from .util import format_countdown, now_ts

log = logging.getLogger(__name__)

EVENTS_PER_ODDS_REQUEST = 20
# How soon to try again when a refresh was cut short. A partial fixture
# list must not be cached for the full EVENTS_REFRESH_SECONDS.
PARTIAL_REFRESH_RETRY_SECONDS = 600
SPORTS_REFRESH_SECONDS = 86400
ALERTS_PER_MESSAGE = 5
STATE_RETENTION_SECONDS = 6 * 3600


def chunked(items: Sequence, size: int) -> Iterable[Sequence]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class Watcher:
    def __init__(
        self,
        config: Config,
        api,  # any provider client from providers.build_client
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
        # Carry the fixture list across restarts: re-listing every sport is
        # one request each, and repaying that on every restart is the single
        # most expensive thing this daemon can do.
        self._scope = "|".join((",".join(config.sports), ",".join(config.leagues)))
        self._events, self._events_fetched_at, self._events_partial = store.load_fixtures(
            self._scope
        )
        if self._events:
            log.info(
                "restored %d fixture(s) from %s, last refreshed %s ago%s",
                len(self._events),
                store.path,
                format_countdown(self.clock() - self._events_fetched_at),
                " (incomplete)" if self._events_partial else "",
            )
        self._all_sports: tuple = ()
        self._sports_fetched_at: float = 0.0
        # Digest mode: alerts accumulate here and go out as one per-player
        # summary every DIGEST_INTERVAL_SECONDS instead of one message each.
        self._digest: list[Alert] = []
        self._digest_started: Optional[float] = None

    # -- fixtures ---------------------------------------------------------
    def refresh_events(self, now: float, *, force: bool = False) -> list[Event]:
        """Re-fetch the fixture list when the cache has gone stale.

        A refresh that the request budget cuts short leaves a fixture list
        missing whole sports, and those fixtures are then invisible: no odds
        are requested for them and nothing they do can alert. Such a list is
        cached only briefly, so the gap closes at the next poll rather than
        lasting the full refresh interval.
        """
        ttl = (
            PARTIAL_REFRESH_RETRY_SECONDS
            if self._events_partial
            else self.config.events_refresh_seconds
        )
        if not force and self._events and now - self._events_fetched_at < ttl:
            return self._events

        events: dict[str, Event] = {}
        sports = self.resolve_sports(now)
        targets = [(sport, league) for sport in sports for league in (self.config.leagues or [None])]
        partial = False
        done = 0
        for sport, league in targets:
            try:
                for event in self.api.get_events(sport, league=league):
                    # Remember which sport a fixture came from: odds are
                    # fetched per sport, and a fixture looked up under the
                    # wrong one silently returns no prices.
                    events[event.id] = (
                        event if event.sport_key else dataclasses.replace(event, sport_key=sport)
                    )
            except BudgetExceeded as exc:
                log.warning("%s", exc)
                partial = True
                break
            except TransportError as exc:
                log.error("failed to load events for sport=%s league=%s: %s", sport, league, exc)
            done += 1

        if events or force:
            self._events = sorted(events.values(), key=lambda e: e.start_ts)
            self._events_fetched_at = now
            self._events_partial = partial
            self.store.save_fixtures(self._events, now, partial=partial, scope=self._scope)
            if partial:
                log.error(
                    "fixture list is INCOMPLETE: %d event(s) from %d of %d sport(s) — the "
                    "request budget ran out. Fixtures in the remaining sports are invisible "
                    "until this retries in %ds. Raise MAX_REQUESTS_PER_HOUR above %d, which "
                    "is what one full refresh costs.",
                    len(self._events), done, len(targets),
                    PARTIAL_REFRESH_RETRY_SECONDS, len(targets),
                )
            else:
                log.info("fixture list refreshed: %d event(s)", len(self._events))
        return self._events

    def resolve_sports(self, now: float) -> tuple:
        """The sports to poll, expanding "all" against the provider's listing.

        The listing changes rarely, so it is fetched once a day rather than on
        every fixture refresh — on a per-request provider each sport costs a
        request per poll, and re-listing them would be pure overhead.
        """
        if not self.config.wants_all_sports:
            return tuple(self.config.sports)
        if self._all_sports and now - self._sports_fetched_at < SPORTS_REFRESH_SECONDS:
            return self._all_sports
        try:
            rows = self.api.get_sports()
        except (TransportError, BudgetExceeded) as exc:
            log.error("could not list sports: %s", exc)
            return self._all_sports
        resolved = tuple(key for key, _title in rows)
        if not resolved:
            # SPORTS=all resolving to nothing means no fixtures, no prices and
            # no alerts — a silence that looks exactly like a quiet evening.
            log.error(
                "SPORTS=all resolved to no sports; the provider's listing was empty "
                "or unreadable. Set SPORTS explicitly, or check `sports`."
            )
            return self._all_sports
        self._all_sports = resolved
        self._sports_fetched_at = now
        log.info("watching %d sport(s): %s", len(self._all_sports), ", ".join(self._all_sports[:8]))
        return self._all_sports

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

        # Group by sport: a provider whose odds endpoint is scoped by sport
        # cannot serve a mixed batch.
        by_sport: dict[str, list[str]] = defaultdict(list)
        for event in events:
            by_sport[event.sport_key].append(event.id)

        sport_scoped = getattr(self.api, "sport_scoped_odds", False)
        batches: list[tuple[str, list]] = []
        for sport, ids in by_sport.items():
            if self.config.per_event_odds:
                batches.extend((sport, [event_id]) for event_id in ids)
            elif sport_scoped:
                # One request already returns every fixture for the sport, and
                # the ids only filter the reply. Splitting them into batches
                # would buy the same payload once per batch.
                batches.append((sport, list(ids)))
            else:
                batches.extend((sport, list(chunk)) for chunk in chunked(ids, EVENTS_PER_ODDS_REQUEST))

        for sport, batch in batches:
            try:
                quotes = (
                    self.api.get_event_odds(batch[0], self.config.bookmakers, sport=sport)
                    if self.config.per_event_odds
                    else self.api.get_multi_odds(batch, self.config.bookmakers, sport=sport)
                )
                for quote in quotes:
                    quotes_by_event[quote.event_id].append(quote)
            except BudgetExceeded as exc:
                log.warning("%s", exc)
                break
            except TransportError as exc:
                log.error("odds request failed: %s", exc)

        alerts: list[Alert] = []
        orphaned = 0
        for event_id, quotes in quotes_by_event.items():
            event = by_id.get(event_id)
            if event is None:
                # A price for a fixture the list does not contain cannot be
                # timed against a kick-off, so it is dropped -- silently, until
                # now. This is what an id mismatch looks like from inside.
                orphaned += len(quotes)
                continue
            alerts.extend(self.detector.process(event, quotes, now))

        if alerts:
            ranked = self.rank_and_cap(alerts)
            self.dispatch(ranked, now)          # per-drop, immediately
            if self.config.digest_interval_seconds > 0:
                self.add_to_digest(ranked, now)  # and roll up for the hourly summary
        self.flush_digest_if_due(now)

        # A poll that finds nothing logs nothing, so a healthy watcher and a
        # stuck one read the same in the journal. Say what was looked at.
        priced = sum(len(q) for q in quotes_by_event.values())
        if orphaned:
            log.warning(
                "%d of %d price(s) belong to fixtures not in the list and were "
                "dropped; %d price(s) were usable",
                orphaned, priced, priced - orphaned,
            )
        log.info(
            "polled %d sport(s), %d fixture(s): %d price(s), %d line(s) tracked, "
            "%d drop(s) >= %.1f%%",
            len(by_sport),
            len(events),
            priced,
            self.store.tracked_lines(),
            len(alerts),
            self.config.min_drop_pct,
        )
        if events and not priced:
            # Name them: an unpriced sport and a broken request look identical
            # until you can see that the sports in range are ones your books
            # do not cover.
            log.warning(
                "no prices came back for %d fixture(s) in range across: %s — "
                "either %s do not price these sports, or the request is wrong. "
                "`verify --sport <one of them>` says which",
                len(events),
                ", ".join(sorted(by_sport)[:12]),
                " / ".join(self.config.bookmakers),
            )

        self.store.purge(now - STATE_RETENTION_SECONDS)
        return alerts

    def rank_and_cap(self, alerts: list[Alert]) -> list[Alert]:
        """Biggest drops first, capped at MAX_ALERTS_PER_POLL.

        Watching every market and player prop can surface hundreds of drops in
        one poll. Sending them all would bury the ones that matter and hit
        Telegram's per-chat rate limit, so the sharpest moves win and the rest
        are logged.
        """
        ranked = sorted(alerts, key=lambda alert: alert.drop_pct, reverse=True)
        cap = self.config.max_alerts_per_poll
        if len(ranked) > cap:
            log.warning(
                "%d drops found, sending the %d largest (raise MIN_DROP_PCT to see fewer)",
                len(ranked),
                cap,
            )
            for alert in ranked[cap:]:
                log.info(
                    "not sent: %s %s %s -%.1f%%",
                    alert.event.name,
                    alert.quote.bookmaker,
                    alert.quote.label,
                    alert.drop_pct,
                )
        return ranked[:cap]

    def add_to_digest(self, alerts: list[Alert], now: float) -> None:
        """Collect already-dispatched alerts for the next hourly summary."""
        if self._digest_started is None:
            self._digest_started = now
        self._digest.extend(alerts)

    def flush_digest_if_due(self, now: float) -> None:
        if not self._digest or self._digest_started is None:
            return
        if now - self._digest_started < self.config.digest_interval_seconds:
            return
        minutes = max(1, self.config.digest_interval_seconds // 60)
        label = "hour" if minutes == 60 else f"{minutes} min"
        text = format_player_digest(
            self._digest, self.config.odds_format, self.config.display_timezone,
            self.config.drop_metric, window_label=label,
        )
        chat = self.config.digest_chat_id or None
        for chunk in split_message(text):
            try:
                self.telegram.send_message(chunk, chat_id=chat)
            except TransportError:
                log.error("could not deliver the hourly digest; will retry next poll")
                return  # keep the buffer, try again on the next poll
        log.info("sent hourly digest: %d move(s)", len(self._digest))
        self._digest = []
        self._digest_started = now

    def dispatch(self, alerts: list[Alert], now: float) -> None:
        """Send one Telegram message per drop, marking each once delivered."""
        for alert in alerts:
            try:
                self.telegram.send_message(
                    format_alert(alert, self.config.odds_format, self.config.display_timezone)
                )
            except TransportError:
                log.error("could not deliver an alert; will retry next poll")
                continue
            self.store.mark_alerted(alert.quote, ts=now)
        self.store.commit()

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
        if self._events_partial:
            # The retry that repairs a truncated fixture list happens inside a
            # poll, so sleeping past it would leave whole sports missing for
            # an idle interval rather than for the retry window. The list is
            # also incomplete, so `upcoming` is computed from partial data.
            wait = min(wait, PARTIAL_REFRESH_RETRY_SECONDS)
        return int(max(cfg.poll_interval_seconds, min(wait, cfg.idle_poll_interval_seconds)))

    def estimate_poll_cost(self, now: float) -> int:
        """Requests one poll costs, at the current slate.

        Odds are requested per sport, but only for the sports that have a
        fixture inside the tracking lead right now — not for every configured
        sport. A wide SPORTS list costs nothing extra at poll time; it costs at
        refresh time, which is a separate, slower clock.
        """
        events = self.events_in_tracking_range(now)
        by_sport: dict[str, int] = defaultdict(int)
        for event in events:
            by_sport[event.sport_key] += 1

        total = 0
        for sport, count in by_sport.items():
            if self.config.per_event_odds:
                calls = count
            elif getattr(self.api, "sport_scoped_odds", False):
                calls = 1
            else:
                calls = -(-count // EVENTS_PER_ODDS_REQUEST)
            total += calls
            # Props are a second request, and PROP_SPORTS may exclude this one.
            if self._props_for(sport):
                total += calls
        return total

    def _props_for(self, sport: str) -> bool:
        wants = getattr(self.api, "wants_props", None)
        if wants is not None:
            return bool(wants(sport))
        return bool(self.config.prop_markets)

    def estimate_refresh_cost(self, sports: int) -> int:
        """Requests one fixture-list refresh costs: every sport, every league."""
        return sports * max(len(self.config.leagues), 1)

    def warn_if_unaffordable(self, now: float) -> None:
        """Say what the configured scope costs before much of it is spent.

        The arithmetic is not obvious from the settings — an allowance can be
        gone in minutes — and the two halves pull in opposite directions: a
        wide SPORTS list multiplies the fixture refresh, while a short
        POLL_INTERVAL_SECONDS multiplies the odds requests.
        """
        cfg = self.config
        sports = len(self.resolve_sports(now))
        per_poll = self.estimate_poll_cost(now)
        per_refresh = self.estimate_refresh_cost(sports)
        polls_per_hour = 3600 / cfg.poll_interval_seconds
        refreshes_per_hour = 3600 / cfg.events_refresh_seconds
        odds_per_hour = per_poll * polls_per_hour
        refresh_per_hour = per_refresh * refreshes_per_hour
        per_hour = int(odds_per_hour + refresh_per_hour)
        in_range = {event.sport_key for event in self.events_in_tracking_range(now)}
        with_props = sum(1 for sport in in_range if self._props_for(sport))
        log.info(
            "cost estimate: %d request(s) per poll (%s, %d sport(s) in range) every %ds "
            "= ~%d/hour, plus %d per fixture refresh (%d sport(s)) every %ds = ~%d/hour; "
            "~%d request(s)/hour in total",
            per_poll,
            f"odds, props on {with_props}" if with_props else "odds",
            len(in_range),
            cfg.poll_interval_seconds,
            int(odds_per_hour),
            per_refresh,
            sports,
            cfg.events_refresh_seconds,
            int(refresh_per_hour),
            per_hour,
        )
        remaining = getattr(self.api, "credits_remaining", None)
        if remaining and per_hour > 0:
            log.warning(
                "provider balance %d lasts about %.1f hour(s) at that rate",
                remaining,
                remaining / per_hour,
            )
        if cfg.per_event_odds and getattr(self.api, "sport_scoped_odds", False):
            log.warning(
                "PER_EVENT_ODDS=true costs one request per fixture on this provider, "
                "but one request already returns the whole sport. Set it to false."
            )
        if per_hour > cfg.max_requests_per_hour:
            log.warning(
                "that exceeds MAX_REQUESTS_PER_HOUR (%d), so requests will be skipped. "
                "Raise EVENTS_REFRESH_SECONDS or POLL_INTERVAL_SECONDS, narrow SPORTS, "
                "or drop PROP_MARKETS.",
                cfg.max_requests_per_hour,
            )

    def run_forever(self) -> None:
        cfg = self.config
        log.info(
            "watching %s on %s | alert when a price drops >= %.1f%% %s",
            "all sports" if cfg.wants_all_sports else ", ".join(cfg.sports),
            ", ".join(cfg.bookmakers),
            cfg.min_drop_pct,
            cfg.alert_window_label,
        )
        first_pass = True
        while True:
            started = self.clock()
            try:
                if first_pass:
                    # Refresh first: the estimate needs the slate to know how
                    # many sports actually have a fixture in range.
                    self.refresh_events(started)
                    self.warn_if_unaffordable(started)
                    first_pass = False
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
