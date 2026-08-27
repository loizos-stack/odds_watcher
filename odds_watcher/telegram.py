"""Telegram Bot API client and alert formatting."""

from __future__ import annotations

import html
import logging
from typing import Optional

from .detector import Alert
from .http import TransportError, build_url, request_json
from .util import format_clock, format_countdown, format_time

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        base_url: str = API_BASE,
        timeout: int = 20,
        dry_run: bool = False,
    ):
        self.token = token
        self.chat_id = chat_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.dry_run = dry_run

    def _url(self, method: str) -> str:
        return build_url(self.base_url, f"bot{self.token}/{method}")

    def send_message(self, text: str, *, disable_preview: bool = True) -> Optional[dict]:
        if self.dry_run:
            log.info("[dry-run] would send to %s:\n%s", self.chat_id, text)
            return None
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        try:
            return request_json(self._url("sendMessage"), method="POST", payload=payload, timeout=self.timeout)
        except TransportError as exc:
            log.error("telegram sendMessage failed: %s", exc)
            raise

    def get_me(self) -> dict:
        return request_json(self._url("getMe"), timeout=self.timeout)

    def get_updates(self) -> list:
        response = request_json(self._url("getUpdates"), timeout=self.timeout) or {}
        return response.get("result", [])


def _esc(value: object) -> str:
    return html.escape(str(value), quote=False)


# The API names moneyline-style outcomes by side, not by team.
_SIDE_ALIASES = {
    "home": "home",
    "1": "home",
    "away": "away",
    "2": "away",
    "draw": "draw",
    "x": "draw",
}


def outcome_label(quote, event) -> str:
    """Readable outcome: the team's name rather than "home"/"away"."""
    side = _SIDE_ALIASES.get(quote.outcome.strip().lower())
    if side == "home":
        return event.home
    if side == "away":
        return event.away
    if side == "draw":
        return "Draw"
    return quote.outcome


def format_price(decimal_odds: float, odds_format: str = "decimal") -> str:
    """Render a price the way the reader configured it.

    Prices are held in decimal internally because a percentage move is only
    meaningful on a continuous scale, but a message that says 1.83 to someone
    who reads -121 all day is a message they have to convert before acting.
    """
    if odds_format != "american":
        return f"{decimal_odds:.2f}"
    from .detector import decimal_to_american

    american = decimal_to_american(decimal_odds)
    return f"{american:+.0f}"


def format_alert(alert: Alert, odds_format: str = "decimal", tz: str = "UTC") -> str:
    """Render one drop as a Telegram HTML message."""
    quote = alert.quote
    event = alert.event
    header = "📉 <b>Odds drop</b>" + (" (continuing)" if alert.is_repeat else "")
    lines = [
        f"{header} · <b>{_esc(quote.bookmaker.upper())}</b>",
        "",
        f"<b>{_esc(event.name)}</b>",
    ]
    context = " · ".join(part for part in (event.sport, event.league) if part)
    if context:
        lines.append(_esc(context))
    market = " ".join(part for part in (quote.market, quote.line) if part)
    lines += [
        f"Kick-off in <b>{format_countdown(alert.seconds_to_start)}</b> "
        f"({_esc(format_clock(event.start_ts, tz))})",
        "",
        f"Market: <b>{_esc(market)}</b> — <b>{_esc(outcome_label(quote, event))}</b>",
        "",
        # Both prices are stamped: which two observations produced this number
        # is the first thing anyone checks before acting on it.
        f"Was:  <s>{_esc(format_price(alert.reference_odds, odds_format))}</s>  "
        f"<i>at {_esc(format_time(alert.reference_ts, tz))}</i>",
        f"Now:  <b>{_esc(format_price(quote.odds, odds_format))}</b>  "
        f"<i>at {_esc(format_time(alert.observed_ts, tz))}</i>",
        f"Drop: <b>{alert.drop_pct:.2f}%</b>",
    ]
    return "\n".join(lines)


def format_digest(alerts: list, odds_format: str = "decimal", tz: str = "UTC") -> str:
    """One message covering several drops found in the same poll."""
    if len(alerts) == 1:
        return format_alert(alerts[0], odds_format, tz)
    blocks = [format_alert(alert, odds_format, tz) for alert in alerts]
    separator = "\n\n" + "—" * 12 + "\n\n"
    return separator.join(blocks)
