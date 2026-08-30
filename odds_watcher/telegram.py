"""Telegram Bot API client and alert formatting."""

from __future__ import annotations

import html
import logging
from typing import Optional

from .detector import Alert
from .http import TransportError, build_url, request_json
from .util import format_clock, format_countdown, format_date, format_time

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
    if decimal_odds is None or decimal_odds <= 1.0:
        return "-"
    if odds_format != "american":
        return f"{decimal_odds:.3f}"
    from .detector import decimal_to_american

    return f"{decimal_to_american(decimal_odds):+.0f}"


_BOOK_NAMES = {
    "bet365": "Bet365",
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betano": "Betano",
    "caesars": "Caesars",
    "betmgm": "BetMGM",
    "pointsbet": "PointsBet",
    "williamhill": "William Hill",
}

_SIDE_WORDS = {"over", "under", "yes", "no"}


def book_name(key: str) -> str:
    """A book's display name; the API keys them lowercase."""
    return _BOOK_NAMES.get(key.strip().lower(), key.strip().title())


def sport_line(event) -> str:
    """"Baseball - MLB" from the event's sport and league, without repetition."""
    sport = (event.sport or "").replace("_", " ").title().strip()
    league = (event.league or "").strip()
    if sport and league and sport.lower() != league.lower():
        return f"{sport} - {league}"
    return league or sport


def humanize_market(market: str) -> str:
    """"player_batter_walks" and "Player Batter Walks" -> "Batter Walks"."""
    name = market.strip()
    low = name.lower()
    for prefix in ("player_", "player "):
        if low.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.replace("_", " ").replace("-", " ").title() or market


def split_prop_outcome(outcome: str):
    """Split a prop outcome into (player, side).

    "Coby Mayo Over" -> ("Coby Mayo", "Over"). A bare side like "Over", or a
    team/other outcome with no side word ("draw"), has no player and returns
    ("", ...) so it is not mistaken for one.
    """
    parts = outcome.strip().split()
    if len(parts) >= 2 and parts[-1].lower() in _SIDE_WORDS:
        return " ".join(parts[:-1]), parts[-1].title()
    return "", ""


def format_line(line: str) -> str:
    """A handicap in parentheses, e.g. "(+0.5)"; empty string when there is none."""
    line = (line or "").strip()
    if not line:
        return ""
    if line[0] in "+-":
        return f"({line})"
    return f"(+{line})"


def format_alert(alert: Alert, odds_format: str = "decimal", tz: str = "UTC") -> str:
    """Render one drop in the update layout: heading, fixture, opening, move, fair."""
    quote = alert.quote
    event = alert.event
    line = format_line(quote.line)

    player, side = split_prop_outcome(quote.outcome)
    if player:
        # A named player -> the player-prop layout, whatever the market label.
        label = f"Player Props - {_esc(player)} ({_esc(humanize_market(quote.market))})"
    else:
        side = outcome_label(quote, event)
        if side.strip().lower() in _SIDE_WORDS:
            side = side.strip().title()
        label = _esc(humanize_market(quote.market))

    heading = "🔁 <b>Odds update" if alert.is_repeat else "🟡 <b>Odds update"
    lines = [
        f"{heading} on {_esc(book_name(quote.bookmaker))}</b>",
        "",
        _esc(sport_line(event)),
        f"<b>{_esc(event.name)}</b>",
        _esc(format_date(event.start_ts, tz)),
        "",
    ]

    line_part = f" {line}" if line else ""
    side_part = f"{_esc(side)} " if side else ""
    opening = alert.opening_odds or alert.reference_odds
    if opening and opening > 1.0:
        # The first price we recorded for this line, before any move.
        lines.append(
            f"🟢 Opening{line_part}: {side_part}"
            f"{_esc(format_price(opening, odds_format))}"
        )
    now = format_price(quote.odds, odds_format)
    lines.append(
        f"🟡 {label}{line_part}: {side_part}"
        f"<b>{_esc(now)}</b> ↓ [-{alert.drop_pct:.1f}%]"
    )

    lines.append("")
    lines.append(
        f"<i>first pitch in {format_countdown(alert.seconds_to_start)}</i>"
    )
    return "\n".join(lines)


def format_digest(alerts: list, odds_format: str = "decimal", tz: str = "UTC") -> str:
    """One message covering several drops found in the same poll."""
    if len(alerts) == 1:
        return format_alert(alerts[0], odds_format, tz)
    blocks = [format_alert(alert, odds_format, tz) for alert in alerts]
    separator = "\n\n" + "—" * 12 + "\n\n"
    return separator.join(blocks)
