"""Client for ParlayAPI (https://parlay-api.com).

Documented surface, from their docs index:

* ``GET /v1/sports``                     - supported sports
* ``GET /v1/sports/{sport}/events``      - upcoming fixtures
* ``GET /v1/sports/{sport}/odds``        - moneyline / spread / total per book
* ``GET /v1/sports/{sport}/props``       - player props
* ``GET /v1/try/{sport}/odds``           - no-auth sample, 60/hour per IP

Authentication is an ``X-API-Key`` header.

The response shapes are not published anywhere reachable from here, so the
parsers below are written to accept the layouts every provider in this project
has used so far, and `probe` dumps the raw payload when nothing matches. Once a
real response is seen, pin it with a fixture rather than widening the guesswork.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from .http import HttpError, build_url, redact, request_json_with_headers
from .odds_api import BudgetExceeded, Event, Quote
from .util import parse_timestamp

log = logging.getLogger(__name__)

BASE_URL = "https://parlay-api.com"
_LIST_KEYS = ("data", "events", "games", "odds", "results", "items", "props", "markets")


def _as_list(payload: Any) -> list:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    return []


def _first(mapping: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(mapping, dict) and name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return default


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(_first(value, "name", "title", "slug", "key", default="")).strip()
    return str(value or "").strip()


def _price(value: Any, odds_format: str = "american") -> Optional[float]:
    """Decimal odds from a price in the configured format.

    ParlayAPI quotes American prices (-130, +120). The format is configured
    rather than inferred, because a decimal longshot of 150.0 and an American
    +150 are indistinguishable by value alone and mean very different things.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if odds_format == "decimal":
        return number if number > 1 else None
    if odds_format == "american":
        if number >= 100:
            return round(1 + number / 100, 4)
        if number <= -100:
            return round(1 + 100 / abs(number), 4)
        return number if number > 1 else None
    # "auto": treat magnitudes of 100+ as American.
    if number >= 100:
        return round(1 + number / 100, 4)
    if number <= -100:
        return round(1 + 100 / abs(number), 4)
    return number if number > 1 else None


def parse_event(raw: dict) -> Optional[Event]:
    if not isinstance(raw, dict):
        return None
    event_id = _first(raw, "id", "event_id", "eventId", "game_id", "gameId")
    start_ts = parse_timestamp(
        _first(raw, "commence_time", "start_time", "startTime", "start", "date", "scheduled")
    )
    if event_id is None or start_ts is None:
        return None
    sport_key = str(_first(raw, "sport_key", "sportKey", "sport", "league_key", default="") or "")
    if isinstance(_first(raw, "sport", default=None), dict):
        sport_key = str(_first(raw["sport"], "key", "slug", default=sport_key))
    return Event(
        id=str(event_id),
        start_ts=start_ts,
        home=_text(_first(raw, "home_team", "homeTeam", "home", "home_name")) or "Home",
        away=_text(_first(raw, "away_team", "awayTeam", "away", "away_name")) or "Away",
        sport=_text(_first(raw, "sport_title", "sportTitle", "sport")),
        sport_key=sport_key,
        league=_text(_first(raw, "league", "competition", "sport_title")),
        league_slug=str(_first(raw, "league_key", "leagueKey", default=sport_key) or ""),
    )


def _iter_books(block: dict):
    """Yield ``(bookmaker, markets container)`` for an event block."""
    books = _first(block, "bookmakers", "books", "sportsbooks", "lines", default=None)
    if isinstance(books, list):
        for item in books:
            if isinstance(item, dict):
                name = _text(_first(item, "key", "book", "bookmaker", "name", "title"))
                yield name.lower(), _first(item, "markets", "odds", "lines", default=item)
    elif isinstance(books, dict):
        for name, value in books.items():
            yield str(name).strip().lower(), value
    else:
        hint = _text(_first(block, "book", "bookmaker", "sportsbook", "key"))
        markets = _first(block, "markets", "odds", "lines", default=None)
        if markets is not None:
            yield hint.lower(), markets


def _iter_markets(container: Any):
    if isinstance(container, list):
        for item in container:
            if isinstance(item, dict):
                yield _text(_first(item, "key", "market", "name", "type", default="market")), item
    elif isinstance(container, dict):
        for name, value in container.items():
            if isinstance(value, (dict, list)):
                yield str(name), value


def _iter_selections(market: Any, odds_format: str = "american"):
    """Yield ``(outcome, price, line, raw)`` from a market node."""
    if isinstance(market, dict):
        base_line = _first(market, "point", "line", "handicap", "total", "hdp", default="")
        selections = _first(
            market, "outcomes", "selections", "runners", "prices", "options", default=None
        )
        if selections is None:
            selections = [market] if _first(market, "price", "odds", default=None) else []
    else:
        base_line, selections = "", market

    for item in _as_list(selections):
        if not isinstance(item, dict):
            continue
        name = _text(_first(item, "name", "outcome", "selection", "side", "label", "team"))
        player = _text(_first(item, "description", "player", "participant"))
        if player and name:
            name = f"{player} {name}"
        elif player:
            name = player
        price = _price(
            _first(item, "price", "odds", "decimal", "decimal_odds", "american"), odds_format
        )
        line = _first(item, "point", "line", "handicap", "total", "hdp", default=base_line)
        if name and price is not None:
            yield name, price, ("" if line in (None, "") else str(line)), item


def parse_quotes(
    payload: Any, *, default_event_id: Optional[str] = None, odds_format: str = "american"
) -> list:
    quotes: list = []
    seen: set = set()
    for block in _as_list(payload):
        if not isinstance(block, dict):
            continue
        event_id = str(
            _first(block, "id", "event_id", "eventId", "game_id", default=default_event_id) or ""
        )
        if not event_id:
            continue
        for book, container in _iter_books(block):
            book_ts = parse_timestamp(
                _first(block, "last_update", "updated_at", default=None)
            )
            for market_name, market in _iter_markets(container):
                market_ts = parse_timestamp(
                    _first(market, "last_update", "updated_at", "timestamp", default=None)
                ) if isinstance(market, dict) else None
                for outcome, price, line, raw in _iter_selections(market, odds_format):
                    bookmaker = (
                        _text(_first(raw, "book", "bookmaker", "sportsbook")).lower() or book
                    )
                    if not bookmaker:
                        continue
                    quote = Quote(
                        event_id=event_id,
                        bookmaker=bookmaker,
                        market=market_name,
                        line=line,
                        outcome=outcome,
                        odds=price,
                        updated_ts=parse_timestamp(
                            _first(raw, "updated_at", "last_update", "timestamp", default=None)
                        )
                        or market_ts
                        or book_ts,
                    )
                    if quote.key in seen:
                        continue
                    seen.add(quote.key)
                    quotes.append(quote)
    return quotes


def market_catalogue(payload: Any, odds_format: str = "american") -> dict:
    catalogue: dict = {}
    for quote in parse_quotes(payload, odds_format=odds_format):
        entry = catalogue.setdefault(quote.market, {})
        entry[quote.bookmaker] = entry.get(quote.bookmaker, 0) + 1
    return catalogue


class ParlayApiClient:
    """ParlayAPI, exposing the interface the watcher expects."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: int = 20,
        budget=None,
        prop_markets: Sequence[str] = (),
        default_sport: str = "",
        odds_format: str = "american",
        **_ignored,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.budget = budget
        self.prop_markets = tuple(prop_markets)
        self.default_sport = default_sport
        self.odds_format = odds_format
        self.supports_multi = True
        self.credits_remaining: Optional[int] = None

    # -- plumbing ---------------------------------------------------------
    def _call(self, path: str, params: Optional[dict] = None, *, metered: bool = True) -> Any:
        if metered and self.budget is not None and not self.budget.try_consume():
            raise BudgetExceeded("local request budget exhausted; skipping call to " + path)
        url = build_url(self.base_url, path, params or {})
        log.debug("GET %s", redact(url))
        data, headers = request_json_with_headers(
            url, timeout=self.timeout, headers={"X-API-Key": self.api_key}
        )
        for header in ("x-ratelimit-remaining", "x-requests-remaining"):
            if header in headers:
                try:
                    self.credits_remaining = int(float(headers[header]))
                except (TypeError, ValueError):
                    pass
                break
        return data

    # -- listings ---------------------------------------------------------
    def get_sports(self, include_all: bool = False) -> list:
        # Nothing documents this endpoint as free, so it is counted. Better to
        # over-report local spend than to quietly overrun a 1,000/month tier.
        rows = []
        for item in _as_list(self._call("v1/sports")):
            if isinstance(item, str):
                rows.append((item, item))
            elif isinstance(item, dict):
                key = _first(item, "key", "sport_key", "slug", "id", "name", default=None)
                if key:
                    rows.append((str(key), _text(_first(item, "title", "name", default=key))))
        return sorted(set(rows))

    def get_leagues(self, sport: str) -> list:
        from .theoddsapi import UnsupportedByProvider

        raise UnsupportedByProvider(
            "ParlayAPI identifies competitions by sport key. Use `sports`."
        )

    def get_bookmakers(self) -> list:
        rows: set = set()
        for block in _as_list(self._sport_odds(self.default_sport or "upcoming")):
            if isinstance(block, dict):
                for book, _markets in _iter_books(block):
                    if book:
                        rows.add((book, book))
        return sorted(rows)

    def get_selected_bookmakers(self) -> list:
        return []

    def select_bookmakers(self, bookmakers: Sequence[str]) -> Any:
        from .theoddsapi import UnsupportedByProvider

        raise UnsupportedByProvider(
            "ParlayAPI returns every book on each request; nothing is selected on the account."
        )

    # -- events and odds --------------------------------------------------
    def get_events(self, sport: str, *, league: Optional[str] = None, limit: Optional[int] = None) -> list:
        payload = self._call(f"v1/sports/{sport}/events")
        events = [parse_event(raw) for raw in _as_list(payload)]
        return [event for event in events if event is not None]

    def _sport_odds(self, sport: str) -> Any:
        return self._call(f"v1/sports/{sport}/odds")

    def _sport_props(self, sport: str) -> Any:
        return self._call(f"v1/sports/{sport}/props")

    def get_odds_payloads(self, event_ids: Sequence[str], bookmakers: Sequence[str],
                          *, sport: str = "", fallback_limit: int = 5) -> list:
        sport = sport or self.default_sport
        if not sport:
            raise ValueError("ParlayAPI needs a sport key to fetch odds")
        wanted = set(event_ids)

        def _keep(block):
            if not isinstance(block, dict):
                return False
            block_id = str(_first(block, "id", "event_id", "eventId", "game_id", default="") or "")
            return not wanted or block_id in wanted

        blocks = [b for b in _as_list(self._sport_odds(sport)) if _keep(b)]
        if self.prop_markets:
            try:
                blocks.extend(b for b in _as_list(self._sport_props(sport)) if _keep(b))
            except HttpError as exc:
                log.warning("props unavailable for %s: %s", sport, exc)
        return blocks

    # -- interface parity -------------------------------------------------
    def get_multi_odds(self, event_ids: Sequence[str], bookmakers: Sequence[str], *, sport: str = "") -> list:
        return self.parse_quotes(self.get_odds_payloads(event_ids, bookmakers, sport=sport))

    def get_event_odds(self, event_id: str, bookmakers: Sequence[str], *, sport: str = "") -> list:
        return self.parse_quotes(
            self.get_event_odds_raw(event_id, bookmakers, sport=sport), default_event_id=event_id
        )

    def get_event_odds_raw(self, event_id: str, bookmakers: Sequence[str], *, sport: str = "") -> Any:
        return self.get_odds_payloads([event_id], bookmakers, sport=sport)

    def get_multi_odds_raw(self, event_ids: Sequence[str], bookmakers: Sequence[str], *, sport: str = "") -> Any:
        return self.get_odds_payloads(event_ids, bookmakers, sport=sport)

    def parse_quotes(self, payload: Any, *, default_event_id: Optional[str] = None) -> list:
        return parse_quotes(
            payload, default_event_id=default_event_id, odds_format=self.odds_format
        )

    def market_catalogue(self, payload: Any) -> dict:
        return market_catalogue(payload, odds_format=self.odds_format)
