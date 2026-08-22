"""Client for the odds-api.io REST API (https://api2.odds-api.io/v3).

Only the handful of endpoints the watcher needs are wrapped:

* ``GET /events``                     - upcoming fixtures per sport/league
* ``GET /odds``                       - odds for one event
* ``GET /odds/multi``                 - odds for several events in one call
* ``GET /bookmakers/selected``        - which books the free tier is bound to
* ``PUT /bookmakers/selected/select`` - bind the account to the chosen books

The response parsers are deliberately forgiving: the API has shipped more than
one payload shape (bare list vs. ``{"data": [...]}``, participant objects vs.
plain ``home``/``away`` strings), so every field is read through a fallback
chain rather than assumed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Sequence

from .http import HttpError, build_url, redact, request_json
from .util import parse_timestamp

log = logging.getLogger(__name__)

_LIST_KEYS = ("data", "events", "results", "items", "odds", "matches")
_MARKET_META_KEYS = {
    "market",
    "name",
    "key",
    "marketLine",
    "line",
    "hdp",
    "handicap",
    "points",
    "bookmaker",
    "book",
    "bookmakerName",
}


@dataclass(frozen=True)
class Event:
    """An upcoming fixture."""

    id: str
    start_ts: float
    home: str
    away: str
    sport: str = ""
    league: str = ""
    league_slug: str = ""

    @property
    def name(self) -> str:
        return f"{self.home} vs {self.away}"

    def seconds_to_start(self, now: float) -> float:
        return self.start_ts - now


@dataclass(frozen=True)
class Quote:
    """A single price offered by one bookmaker on one outcome."""

    event_id: str
    bookmaker: str
    market: str
    line: str
    outcome: str
    odds: float
    updated_ts: Optional[float] = None

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.event_id, self.bookmaker, self.market, self.line, self.outcome)

    @property
    def label(self) -> str:
        return f"{self.market} {self.line} · {self.outcome}".replace("  ", " ").strip()


class BudgetExceeded(RuntimeError):
    """The local request budget for the free tier is used up."""


def _as_list(payload: Any) -> list:
    """Unwrap the many container shapes the API may answer with."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        # A single object (e.g. /odds for one event) is a one-item list.
        return [payload]
    return []


def _first(mapping: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return default


def _participant(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(_first(raw, "name", "title", "shortName", default="")).strip()
    return str(raw or "").strip()


def parse_event(raw: dict) -> Optional[Event]:
    """Turn one raw fixture into an :class:`Event`, or None if unusable."""
    if not isinstance(raw, dict):
        return None
    event_id = _first(raw, "id", "eventId", "event_id", "matchId")
    start_ts = parse_timestamp(
        _first(raw, "startTime", "start_time", "date", "startsAt", "commence_time", "time")
    )
    home = _participant(_first(raw, "homeParticipant", "home", "homeTeam", "home_team"))
    away = _participant(_first(raw, "awayParticipant", "away", "awayTeam", "away_team"))
    if event_id is None or start_ts is None:
        log.debug("skipping event without id/start time: %s", raw)
        return None

    def _name(value: Any) -> str:
        if isinstance(value, dict):
            return str(_first(value, "name", "slug", default="")).strip()
        return str(value or "").strip()

    league_raw = _first(raw, "league", "competition", "leagueName")
    return Event(
        id=str(event_id),
        start_ts=start_ts,
        home=home or "Home",
        away=away or "Away",
        sport=_name(raw.get("sport")),
        league=_name(league_raw),
        league_slug=str(league_raw.get("slug", "")) if isinstance(league_raw, dict) else "",
    )


# Keys inside an odds row that describe the row rather than name an outcome.
_ROW_META_KEYS = {"hdp", "handicap", "line", "points", "label", "name", "updatedAt", "updated_at"}


def _iter_outcomes(node: Any) -> Iterator[tuple[str, Any, dict]]:
    """Yield ``(outcome_name, price, raw)`` from an outcomes collection."""
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                name = str(_first(item, "name", "outcome", "betSide", "side", "label", default="")).strip()
                price = _first(item, "odds", "price", "value", "decimal", "decimalOdds")
                if name:
                    yield name, price, item
    elif isinstance(node, dict):
        for name, value in node.items():
            if isinstance(value, dict):
                price = _first(value, "odds", "price", "value", "decimal", "decimalOdds")
                yield str(name), price, value
            elif isinstance(value, (int, float, str)):
                yield str(name), value, {}


def _iter_market_prices(market_node: dict) -> Iterator[tuple[str, str, Any, dict]]:
    """Yield ``(line, outcome, price, raw)`` for every price in one market.

    Three layouts are supported, in priority order:

    1. ``odds`` as a list of rows — the live odds-api.io shape. Each row is one
       line of the market: ``{"hdp": -1.25, "home": "2.000", "away": "1.800"}``,
       or a labelled single price ``{"label": "Home / Home", "odds": "2.200"}``.
       Several rows mean several handicaps of the same market.
    2. An explicit ``outcomes`` / ``selections`` / ``runners`` collection.
    3. Prices hanging directly off the market node.
    """
    base_line = _first(market_node, "marketLine", "line", "hdp", "handicap", "points", default="")
    base_line = "" if base_line in (None, "") else str(base_line)

    rows = market_node.get("odds")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_line = _first(row, "hdp", "handicap", "line", "points", default=base_line)
            line = "" if raw_line in (None, "") else str(raw_line)
            label = _first(row, "label", "name", default=None)
            if label is not None and "odds" in row:
                # A single labelled price, e.g. Half Time / Full Time.
                yield line, str(label), row["odds"], row
                continue
            for key, value in row.items():
                if key in _ROW_META_KEYS:
                    continue
                if isinstance(value, (int, float, str)):
                    yield line, str(key), value, row
        return

    outcomes = _first(market_node, "outcomes", "selections", "runners", "prices", default=None)
    if outcomes is not None:
        for name, price, raw in _iter_outcomes(outcomes):
            yield base_line, name, price, raw
        return

    leftovers = {k: v for k, v in market_node.items() if k not in _MARKET_META_KEYS}
    for name, price, raw in _iter_outcomes(leftovers):
        yield base_line, name, price, raw


def _to_float(value: Any) -> Optional[float]:
    """Decimal odds as a float. Prices arrive as strings ("1.420") in places."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _market_nodes(node: Any) -> Iterator[tuple[str, dict]]:
    """Yield ``(market_name, market_node)`` from a list- or dict-shaped block."""
    if isinstance(node, dict):
        for name, value in node.items():
            if isinstance(value, dict):
                yield str(name), value
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, dict):
                yield str(_first(value, "market", "name", "key", default="market")), value


def _bookmaker_sections(block: dict) -> Iterator[tuple[str, Any]]:
    """Yield ``(bookmaker_hint, markets_container)`` pairs for one event block.

    Handles both layouts seen in the wild: markets at the top of the event with
    the bookmaker named on each price, and markets nested under a bookmaker.
    """
    hint = str(_first(block, "bookmaker", "book", "bookmakerName", default="") or "").strip().lower()
    markets = _first(block, "markets", "odds", default=None)
    if markets is not None:
        yield hint, markets

    books = block.get("bookmakers")
    if isinstance(books, dict):
        for name, value in books.items():
            nested = value.get("markets", value) if isinstance(value, dict) else value
            yield str(name).strip().lower(), nested
    elif isinstance(books, list):
        for item in books:
            if isinstance(item, dict):
                name = str(_first(item, "name", "bookmaker", "key", "slug", default="") or "").strip().lower()
                yield name, _first(item, "markets", "odds", default=[])


def parse_quotes(payload: Any, *, default_event_id: Optional[str] = None) -> list[Quote]:
    """Flatten an odds payload into individual :class:`Quote` objects."""
    quotes: list[Quote] = []
    seen: set = set()
    for block in _as_list(payload):
        if not isinstance(block, dict):
            continue
        event_id = _first(block, "eventId", "event_id", "id", default=default_event_id)
        if event_id is None:
            continue

        for hint, markets in _bookmaker_sections(block):
            for market_name, market_node in _market_nodes(markets):
                market_ts = parse_timestamp(
                    _first(market_node, "updatedAt", "updated_at", "timestamp", default=None)
                )
                for line, outcome_name, price, raw in _iter_market_prices(market_node):
                    odds = _to_float(price)
                    if odds is None:
                        continue
                    bookmaker = str(
                        _first(raw, "bookmaker", "book", "bookmakerName", default=None)
                        or _first(market_node, "bookmaker", "book", "bookmakerName", default=None)
                        or hint
                    ).strip().lower()
                    if not bookmaker:
                        continue
                    quote = Quote(
                        event_id=str(event_id),
                        bookmaker=bookmaker,
                        market=str(market_name),
                        line=line,
                        outcome=str(outcome_name),
                        odds=odds,
                        updated_ts=parse_timestamp(
                            _first(raw, "timestamp", "updatedAt", "lastUpdate", default=None)
                        )
                        or market_ts,
                    )
                    if quote.key in seen:
                        continue
                    seen.add(quote.key)
                    quotes.append(quote)
    return quotes


def market_catalogue(payload: Any) -> dict:
    """``{market name: {bookmaker: number of prices}}`` for a raw odds payload.

    Read from the payload structure rather than from parsed quotes, so a market
    still shows up in the listing even if its prices fail to parse.
    """
    catalogue: dict = {}
    for block in _as_list(payload):
        if not isinstance(block, dict):
            continue
        for hint, markets in _bookmaker_sections(block):
            for market_name, market_node in _market_nodes(markets):
                prices = sum(1 for _ in _iter_market_prices(market_node))
                if not prices:
                    continue
                entry = catalogue.setdefault(str(market_name), {})
                book = hint or "?"
                entry[book] = entry.get(book, 0) + prices
    return catalogue


class OddsApiClient:
    """Thin, budget-aware wrapper around the odds-api.io endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api2.odds-api.io/v3",
        timeout: int = 20,
        budget=None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.budget = budget
        self.supports_multi = True

    # -- plumbing ---------------------------------------------------------
    def _call(self, path: str, params: Optional[dict] = None, *, method: str = "GET") -> Any:
        if self.budget is not None and not self.budget.try_consume():
            raise BudgetExceeded(
                "local API request budget exhausted; skipping call to " + path
            )
        query = {"apiKey": self.api_key, **(params or {})}
        url = build_url(self.base_url, path, query)
        log.debug("%s %s", method, redact(url))
        return request_json(url, method=method, timeout=self.timeout)

    # -- endpoints --------------------------------------------------------
    def get_events(
        self,
        sport: str,
        *,
        league: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Event]:
        payload = self._call("events", {"sport": sport, "league": league, "limit": limit})
        events = [parse_event(raw) for raw in _as_list(payload)]
        return [event for event in events if event is not None]

    def get_event_odds_raw(self, event_id: str, bookmakers: Sequence[str]) -> Any:
        """The undecoded /odds payload, for diagnostics."""
        return self._call("odds", {"eventId": event_id, "bookmakers": ",".join(bookmakers)})

    def get_event_odds(self, event_id: str, bookmakers: Sequence[str]) -> list[Quote]:
        payload = self.get_event_odds_raw(event_id, bookmakers)
        return parse_quotes(payload, default_event_id=event_id)

    def get_multi_odds_raw(self, event_ids: Sequence[str], bookmakers: Sequence[str]) -> Any:
        """The undecoded /odds/multi payload, for diagnostics and discovery."""
        return self._call(
            "odds/multi",
            {"eventIds": ",".join(event_ids), "bookmakers": ",".join(bookmakers)},
        )

    def get_multi_odds(self, event_ids: Sequence[str], bookmakers: Sequence[str]) -> list[Quote]:
        """Odds for several events in a single request.

        Falls back to one call per event when the endpoint is unavailable on
        the account's plan, so the watcher keeps working either way.
        """
        if not event_ids:
            return []
        if self.supports_multi:
            try:
                payload = self._call(
                    "odds/multi",
                    {"eventIds": ",".join(event_ids), "bookmakers": ",".join(bookmakers)},
                )
                return parse_quotes(payload)
            except HttpError as exc:
                if exc.status not in (400, 401, 403, 404):
                    raise
                log.warning(
                    "odds/multi unavailable (HTTP %s); falling back to per-event requests",
                    exc.status,
                )
                self.supports_multi = False

        quotes: list[Quote] = []
        for event_id in event_ids:
            quotes.extend(self.get_event_odds(event_id, bookmakers))
        return quotes

    def get_leagues(self, sport: str) -> list[tuple[str, str]]:
        """Leagues for a sport, as ``(identifier, display name)`` for LEAGUES."""
        payload = self._call("leagues", {"sport": sport})
        rows: list[tuple[str, str]] = []
        for item in _as_list(payload):
            if isinstance(item, str):
                rows.append((item, item))
            elif isinstance(item, dict):
                identifier = _first(item, "slug", "id", "key", "league", "name", default=None)
                if identifier is None:
                    continue
                label = str(_first(item, "name", "title", "slug", default=identifier))
                rows.append((str(identifier), label))
        return sorted(set(rows))

    def get_bookmakers(self) -> list[tuple[str, str]]:
        """All bookmakers the API knows, as ``(identifier, display name)``.

        The identifier is what /bookmakers/selected/select expects, and it is
        not always the obvious lowercase name — hence this listing.
        """
        payload = self._call("bookmakers")
        rows: list[tuple[str, str]] = []
        for item in _as_list(payload):
            if isinstance(item, str):
                rows.append((item, item))
            elif isinstance(item, dict):
                identifier = _first(item, "id", "slug", "key", "bookmaker", "name", default=None)
                if identifier is None:
                    continue
                label = str(_first(item, "name", "title", "slug", default=identifier))
                rows.append((str(identifier), label))
        return sorted(set(rows))

    def get_odds_payloads(
        self, event_ids: Sequence[str], bookmakers: Sequence[str], *, fallback_limit: int = 5
    ) -> list:
        """Raw odds blocks for several fixtures, batched where the plan allows.

        Returns one block per fixture that answered. Falls back to individual
        requests when /odds/multi is unavailable, capped so a diagnostic can
        never burn the whole hourly allowance.
        """
        if not event_ids:
            return []
        if self.supports_multi:
            try:
                return _as_list(self.get_multi_odds_raw(event_ids, bookmakers))
            except HttpError as exc:
                if exc.status not in (400, 401, 403, 404):
                    raise
                log.warning(
                    "odds/multi unavailable (HTTP %s); sampling %d fixture(s) individually",
                    exc.status,
                    min(len(event_ids), fallback_limit),
                )
                self.supports_multi = False

        blocks = []
        for event_id in list(event_ids)[:fallback_limit]:
            blocks.extend(_as_list(self.get_event_odds_raw(event_id, bookmakers)))
        return blocks

    def get_selected_bookmakers(self) -> list[str]:
        payload = self._call("bookmakers/selected")
        if isinstance(payload, dict):
            for key in ("selected", "bookmakers", "data", "result", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    payload = value
                    break
        names: list[str] = []
        for item in _as_list(payload):
            if isinstance(item, str):
                names.append(item.lower())
            elif isinstance(item, dict):
                name = _first(item, "name", "slug", "id", "key", "bookmaker", "title", default=None)
                if isinstance(name, dict):
                    name = _first(name, "name", "slug", "id", default=None)
                if name:
                    names.append(str(name).lower())
        return names

    def select_bookmakers(self, bookmakers: Sequence[str]) -> Any:
        return self._call(
            "bookmakers/selected/select",
            {"bookmakers": ",".join(bookmakers)},
            method="PUT",
        )
