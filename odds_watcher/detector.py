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


def decimal_to_american(decimal: float) -> float:
    """The American price for a decimal one. 1.91 -> -110, 2.50 -> +150."""
    if decimal >= 2.0:
        return (decimal - 1.0) * 100.0
    return -100.0 / (decimal - 1.0)


def drop_pct(reference: float, current: float) -> float:
    """Percentage the price has shortened by (negative when it drifted out)."""
    if reference <= 0:
        return 0.0
    return (reference - current) / reference * 100.0


def american_drop_pct(reference: float, current: float) -> float:
    """The same move, measured the way a bettor quotes it.

    A move from -110 to -121 is "ten percent" at the counter, but the decimal
    prices are 1.9091 and 1.8264 -- a 4.33% drop. The two scales differ by
    more than a factor of two, so a threshold set in one and measured in the
    other never fires.

    Around even money the American scale has a discontinuity: +105 and -105
    are adjacent prices, yet identical in magnitude. A move that crosses it is
    measured in decimal, where the ordering is continuous.
    """
    if reference <= 1.0 or current <= 1.0:
        return 0.0
    ref_american = decimal_to_american(reference)
    now_american = decimal_to_american(current)
    if (ref_american > 0) != (now_american > 0):
        return drop_pct(reference, current)
    change = abs(now_american) / abs(ref_american) - 1.0
    # A favourite shortens as its number grows (-110 -> -121); an underdog
    # shortens as its number shrinks (+150 -> +130).
    return change * 100.0 if ref_american < 0 else -change * 100.0


def measure_drop(reference: float, current: float, metric: str) -> float:
    """Shortening of `reference` to `current`, on the configured scale."""
    if metric == "american":
        return american_drop_pct(reference, current)
    return drop_pct(reference, current)


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

        first_seen_mode = cfg.baseline_mode == "first-seen"
        last_seen_mode = cfg.baseline_mode == "last-seen"
        if last_seen_mode:
            # Compare each price with the one recorded before it: the most
            # sensitive rule, catching a move between any two consecutive polls.
            pre_window = True
            in_window = cfg.window_end_seconds <= seconds <= cfg.window_start_seconds
        elif first_seen_mode:
            # The first price recorded is the reference and never moves, so the
            # whole tracked period is alertable.
            pre_window = False
            in_window = cfg.window_end_seconds <= seconds <= cfg.baseline_lead_seconds
        else:
            pre_window = seconds > cfg.window_start_seconds
            in_window = cfg.window_end_seconds <= seconds <= cfg.window_start_seconds
        alerts: list[Alert] = []

        for quote in quotes:
            if quote.event_id != event.id or not self.relevant(quote):
                continue

            state = self.store.get_state(quote)
            self.store.record(
                quote,
                pre_window=pre_window,
                event_start=event.start_ts,
                ts=now,
                existing=state,
                event_name=event.name,
            )

            has_reference = state is not None and (
                first_seen_mode or last_seen_mode or state.baseline_pre_window
            )
            if not in_window or not has_reference:
                continue

            reference = state.last_odds if last_seen_mode else state.reference_odds
            change = measure_drop(reference, quote.odds, cfg.drop_metric)
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

        # One commit for the whole fixture rather than one per price: with all
        # markets and player props enabled this is thousands of rows a poll.
        self.store.commit()
        return alerts
