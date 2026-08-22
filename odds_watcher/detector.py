"""Drop detection: decide which quotes deserve a Telegram alert.

The rule implemented here is the one the bot exists for:

    alert when the price of an outcome at either watched bookmaker falls by at least
    ``MIN_DROP_PCT`` **during the last 10 minutes before kick-off**, measured
    against the price that stood when the event entered that window.

Prices are therefore recorded from ``BASELINE_LEAD_SECONDS`` before kick-off so
a reference price exists by the time the window opens; while the event is still
outside the window the baseline keeps following the newest price.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

from .config import Config
from .odds_api import Event, Quote
from .store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    event: Event
    quote: Quote
    reference_odds: float
    drop_pct: float
    seconds_to_start: float
    repeat: bool = False

    @property
    def is_repeat(self) -> bool:
        return self.repeat


def drop_pct(reference: float, current: float) -> float:
    """Percentage the price has shortened by (negative when it drifted out)."""
    if reference <= 0:
        return 0.0
    return (reference - current) / reference * 100.0


def market_allowed(market: str, wanted: Sequence[str]) -> bool:
    """Case-insensitive substring match, with exclusions.

    An entry prefixed with ``-`` or ``!`` excludes: ``Totals,-HT`` keeps
    "Totals" and "Corner Totals" but drops "Totals HT". Exclusions win, and a
    config of exclusions only means "everything except these".
    """
    name = market.strip().lower()
    includes = [w.strip().lower() for w in wanted if not w.startswith(("-", "!"))]
    excludes = [w.strip("-!").strip().lower() for w in wanted if w.startswith(("-", "!"))]

    if any(pattern and pattern in name for pattern in excludes):
        return False
    if not includes:
        return True
    return any(pattern == name or pattern in name for pattern in includes)


def outcome_allowed(outcome: str, wanted: Sequence[str]) -> bool:
    """Restrict which sides may alert, e.g. OUTCOMES=over,under for totals."""
    if not wanted:
        return True
    name = outcome.strip().lower()
    return any(w.strip().lower() == name for w in wanted)


class DropDetector:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.bookmakers = {b.strip().lower() for b in config.bookmakers}

    def relevant(self, quote: Quote) -> bool:
        return (
            quote.bookmaker in self.bookmakers
            and market_allowed(quote.market, self.config.markets)
            and outcome_allowed(quote.outcome, self.config.outcomes)
            and quote.odds >= self.config.min_odds
        )

    def process(self, event: Event, quotes: Iterable[Quote], now: float) -> list[Alert]:
        """Record every relevant quote and return the alerts it triggered.

        Alerts are *not* marked as sent here — the caller does that once
        Telegram has accepted the message, so a delivery failure is retried on
        the next poll instead of being silently swallowed.
        """
        cfg = self.config
        seconds = event.seconds_to_start(now)

        if seconds > cfg.baseline_lead_seconds or seconds < cfg.window_end_seconds:
            # Too early to bother, or the window has already closed.
            return []

        pre_window = seconds > cfg.window_start_seconds
        in_window = cfg.window_end_seconds <= seconds <= cfg.window_start_seconds
        alerts: list[Alert] = []

        for quote in quotes:
            if quote.event_id != event.id or not self.relevant(quote):
                continue

            state = self.store.get_state(quote)
            self.store.record(quote, pre_window=pre_window, event_start=event.start_ts, ts=now)

            if not in_window or state is None or not state.baseline_pre_window:
                continue

            reference = state.reference_odds
            change = drop_pct(reference, quote.odds)
            if change < cfg.min_drop_pct:
                continue

            alerts.append(
                Alert(
                    event=event,
                    quote=quote,
                    reference_odds=reference,
                    drop_pct=change,
                    seconds_to_start=seconds,
                    repeat=state.alert_count > 0,
                )
            )
            log.info(
                "drop detected: %s %s %s %.2f -> %.2f (-%.1f%%)",
                event.name,
                quote.bookmaker,
                quote.label,
                reference,
                quote.odds,
                change,
            )

        return alerts
