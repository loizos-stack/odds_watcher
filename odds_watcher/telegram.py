"""Telegram Bot API client and alert formatting."""

from __future__ import annotations

import html
import logging
from typing import Optional

from .detector import Alert, measure_drop
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

    def send_message(self, text: str, *, disable_preview: bool = True,
                     chat_id: Optional[str] = None) -> Optional[dict]:
        target = chat_id or self.chat_id
        if self.dry_run:
            log.info("[dry-run] would send to %s:\n%s", target, text)
            return None
        payload = {
            "chat_id": target,
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
        return f"{decimal_odds:.2f}"
    from .detector import decimal_to_american

    return f"{decimal_to_american(decimal_odds):+.0f}"


_BOOK_NAMES = {
    "bet365": "Bet365",
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "prophetx": "ProphetX",
    "novig": "Novig",
    "betano": "Betano",
    "caesars": "Caesars",
    "betmgm": "BetMGM",
    "pointsbet": "PointsBet",
    "williamhill": "William Hill",
}

_SIDE_WORDS = {"over", "under", "yes", "no"}

# Telegram HTML has no text colour, so a book's colour is shown as a square
# next to its name.
_BOOK_COLOURS = {
    "bet365": "🟩",
    "draftkings": "🟥",
    "fanduel": "🟦",
    "prophetx": "🟪",
    "novig": "⬛",
    "betmgm": "🟨",
}


def severity_dot(drop_pct: float) -> str:
    """Colour a drop by size: 4-10% yellow, 10.01-15% orange, 15.01%+ red."""
    if drop_pct <= 10.0:
        return "🟡"
    if drop_pct <= 15.0:
        return "🟠"
    return "🔴"


def book_name(key: str) -> str:
    """A book's display name; the API keys them lowercase."""
    return _BOOK_NAMES.get(key.strip().lower(), key.strip().title())


def book_label(key: str) -> str:
    """Display name with its colour square, e.g. "🟩 Bet365"."""
    square = _BOOK_COLOURS.get(key.strip().lower())
    name = book_name(key)
    return f"{square} {name}" if square else name


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

    dot = severity_dot(alert.drop_pct)
    tail = " ↻" if alert.is_repeat else ""
    lines = [
        f"{dot} <b>Odds update on {_esc(book_label(quote.bookmaker))}</b>{tail}",
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


def format_player_digest(alerts, odds_format="decimal", tz="UTC", metric="decimal",
                         window_label=""):
    """An hour (or window) of drops, grouped by player, with the drop percentage.

    Reads like:

        📊 Summary — last 30 min
        6 drops · 3 players
        ──────────────
        👤 Jared Triolo
        🏟 Pirates vs Brewers · 31.08.2026 19:05
        🟡 Over 0.5 Hits   1.34 → 1.30   -4.3%   DraftKings
        🟠 Over 0.5 Home Runs   8.43 → 7.50   -12.4%   Bet365

    A line that dropped several times keeps its earliest opening and latest
    price, so each entry is one net move.
    """
    players: dict = {}
    events: dict = {}
    order: list = []
    for a in alerts:
        player, side = split_prop_outcome(a.quote.outcome)
        who = player or outcome_label(a.quote, a.event)
        key = (a.quote.bookmaker, a.quote.market, a.quote.line, side or a.quote.outcome)
        opening = a.opening_odds or a.reference_odds
        opening_ts = a.opening_ts or a.observed_ts
        if who not in players:
            players[who] = {}
            events[who] = a.event
            order.append(who)
        rec = players[who].get(key)
        if rec is None:
            players[who][key] = {
                "open": opening, "open_ts": opening_ts,
                "last": a.quote.odds, "last_ts": a.observed_ts,
                "side": side, "market": a.quote.market,
                "line": a.quote.line, "book": a.quote.bookmaker,
            }
        else:
            if opening_ts < rec["open_ts"]:
                rec["open"], rec["open_ts"] = opening, opening_ts
            if a.observed_ts >= rec["last_ts"]:
                rec["last"], rec["last_ts"] = a.quote.odds, a.observed_ts

    moves = sum(len(v) for v in players.values())
    title = f"📊 <b>Summary — last {window_label}</b>" if window_label else "📊 <b>Summary</b>"
    out = [title, f"{moves} drop(s) · {len(order)} player(s)", "──────────────"]
    for who in order:
        # Biggest mover first within each player.
        rows = sorted(
            players[who].values(),
            key=lambda r: measure_drop(r["open"], r["last"], metric),
            reverse=True,
        )
        out.append(f"👤 <b>{_esc(who)}</b>")
        event = events.get(who)
        if event is not None and getattr(event, "name", ""):
            matchup = f"🏟 <b>{_esc(event.name)}</b>"
            when = format_date(event.start_ts, tz) if event.start_ts else ""
            out.append(f"{matchup} · {_esc(when)}" if when else matchup)
        for rec in rows:
            line = (rec["line"] or "").strip()
            line = f"{line} " if line else ""
            side = f"{rec['side']} " if rec["side"] else ""
            net = measure_drop(rec["open"], rec["last"], metric)
            out.append(
                f"{severity_dot(net)} {_esc(side)}{_esc(line)}"
                f"{_esc(humanize_market(rec['market']))}   "
                f"{_esc(format_price(rec['open'], odds_format))} → "
                f"<b>{_esc(format_price(rec['last'], odds_format))}</b>   "
                f"<b>-{net:.1f}%</b>   {_esc(book_label(rec['book']))}"
            )
        out.append("")
    return "\n".join(out).rstrip()


def split_message(text: str, limit: int = 3800):
    """Split a long digest into Telegram-sized messages on blank-line seams."""
    blocks = text.split("\n\n")
    chunks: list = []
    current = ""
    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def format_digest(alerts: list, odds_format: str = "decimal", tz: str = "UTC") -> str:
    """One message covering several drops found in the same poll."""
    if len(alerts) == 1:
        return format_alert(alerts[0], odds_format, tz)
    blocks = [format_alert(alert, odds_format, tz) for alert in alerts]
    separator = "\n\n" + "—" * 12 + "\n\n"
    return separator.join(blocks)
