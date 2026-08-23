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

# Markets every sport accepts. Used as the fallback when a wider request is
# rejected, and as the starting point for "all".
# The valid-market list is global to the API, not per sport, so it is
# cached under a key no sport slug can collide with.
GLOBAL_MARKET_KEYS = "__parlay_game_markets__"
CORE_GAME_MARKETS = ("h2h", "spreads", "totals")


def valid_markets_from_error(message: str) -> tuple:
    """The market keys an INVALID_MARKET response lists as acceptable.

    The API answers a bad market with the full set it will accept, which is a
    better source than any list hard-coded here.
    """
    marker = "Valid values are:"
    if marker not in message:
        return ()
    tail = message.split(marker, 1)[1]
    tail = tail.split(";")[0]
    keys = []
    for part in tail.replace("\\n", " ").split(","):
        key = part.strip().strip("'\"").strip()
        key = key.split(" ")[0].strip("'\".")
        if key and all(ch.isalnum() or ch == "_" for ch in key):
            keys.append(key)
    return tuple(dict.fromkeys(keys))
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


def _int(value: Any, default=None):
    try:
        return int(float(value))
    except (TypeError, ValueError):
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


def _flat_prop_quotes(row: dict, default_event_id: Optional[str], odds_format: str) -> list:
    """Quotes from a flat prop row.

    The props endpoint returns one row per player/line/book rather than nested
    markets: ``{"player_name": ..., "line": 0.5, "over_price": -110,
    "under_price": -120, "bookmaker": "draftkings"}``.
    """
    event_id = str(
        _first(row, "event_id", "eventId", "game_id", "id", default=default_event_id) or ""
    )
    book = _text(_first(row, "bookmaker", "book", "sportsbook")).lower()
    player = _text(_first(row, "player_name", "player", "participant", "description"))
    market = _text(_first(row, "market", "market_key", "marketKey", "key", "prop_type"))
    if not (event_id and book and market):
        return []
    line_raw = _first(row, "line", "point", "handicap", "total", default="")
    line = "" if line_raw in (None, "") else str(line_raw)

    quotes = []
    for field, side in (("over_price", "Over"), ("under_price", "Under"), ("price", "")):
        if field not in row:
            continue
        price = _price(row[field], odds_format)
        if price is None:
            continue
        name = _text(_first(row, "name", "outcome", "selection", default="")) if not side else side
        outcome = " ".join(part for part in (player, name) if part) or name or player
        if not outcome:
            continue
        quotes.append(
            Quote(
                event_id=event_id,
                bookmaker=book,
                market=market,
                line=line,
                outcome=outcome,
                odds=price,
                updated_ts=parse_timestamp(
                    _first(row, "updated_at", "last_update", "timestamp", default=None)
                ),
            )
        )
    return quotes


def parse_quotes(
    payload: Any, *, default_event_id: Optional[str] = None, odds_format: str = "american"
) -> list:
    quotes: list = []
    seen: set = set()
    for block in _as_list(payload):
        if not isinstance(block, dict):
            continue
        # A flat prop row carries its prices directly rather than nested books.
        if any(key in block for key in ("over_price", "under_price")) or (
            "player_name" in block and "bookmaker" in block
        ):
            for quote in _flat_prop_quotes(block, default_event_id, odds_format):
                if quote.key not in seen:
                    seen.add(quote.key)
                    quotes.append(quote)
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
        regions: Sequence[str] = (),
        featured_markets: Sequence[str] = ("h2h", "spreads", "totals"),
        bookmakers: Sequence[str] = (),
        market_cache=None,
        market_keys_ttl: float = 86400.0,
        **_ignored,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.budget = budget
        self.prop_markets = tuple(prop_markets)
        self.regions = tuple(regions)
        self.featured_markets = tuple(featured_markets)
        self.bookmakers = tuple(bookmakers)
        self.default_sport = default_sport
        self.odds_format = odds_format
        self.supports_multi = True
        # One /odds call returns every fixture for the sport; event ids only
        # filter the reply, so the watcher must not split them into batches.
        self.sport_scoped_odds = True
        self.credits_remaining: Optional[int] = None
        # Learned market keys survive a restart: rediscovering them costs a
        # rejected request per sport, every run.
        self.market_cache = market_cache
        self.market_keys_ttl = market_keys_ttl
        self._game_market_cache: dict = {}
        # The API's valid-market list is global, so one rejection teaches every
        # sport. Probing per sport would waste a request on each of them.
        self._valid_markets: tuple = ()
        self.last_usage_payload: Any = None

    # -- plumbing ---------------------------------------------------------
    def _call(self, path: str, params: Optional[dict] = None, *, metered: bool = True) -> Any:
        if metered and self.budget is not None and not self.budget.try_consume():
            raise BudgetExceeded("local request budget exhausted; skipping call to " + path)
        url = build_url(self.base_url, path, params or {})
        log.debug("GET %s", redact(url))
        data, headers = request_json_with_headers(
            url, timeout=self.timeout, headers={"X-API-Key": self.api_key}
        )
        self._read_allowance(data, headers)
        return data

    def _read_allowance(self, data: Any, headers: dict) -> None:
        """Record the remaining monthly allowance.

        The body is preferred over headers. A rate-limit header reports
        requests per second, not the account balance, and reading one as the
        other understates a healthy account by an order of magnitude.
        """
        if isinstance(data, dict):
            for field in (
                "credits_remaining",
                "requests_remaining",
                "remaining_requests",
                "quota_remaining",
                "remaining",
                "demo_remaining_hour",
            ):
                if field in data:
                    value = _int(data[field])
                    if value is not None:
                        self.credits_remaining = value
                        return
        for header in (
            "x-credits-remaining",
            "x-requests-remaining",
            "x-ratelimit-remaining-month",
        ):
            if header in headers:
                value = _int(headers[header])
                if value is not None:
                    self.credits_remaining = value
                    return

    def fetch_quota(self) -> dict:
        """The account's remaining allowance from the dedicated usage endpoint.

        Deliberately bypasses the local cap: being unable to find out how much
        is left because the local cap is spent is exactly backwards.
        """
        data = self._call("v1/usage", metered=False)
        self.last_usage_payload = data
        remaining = self.credits_remaining
        used = None
        limit = None
        if isinstance(data, dict):
            body = data.get("usage") if isinstance(data.get("usage"), dict) else data
            for field in ("credits_remaining", "remaining", "requests_remaining",
                          "remaining_requests"):
                if field in body:
                    remaining = _int(body[field], remaining)
                    break
            for field in ("credits_used", "used", "requests_used", "count"):
                if field in body:
                    used = _int(body[field], used)
                    break
            for field in ("credits_total", "limit", "quota", "monthly_limit",
                          "requests_limit"):
                if field in body:
                    limit = _int(body[field], limit)
                    break
            resets = body.get("period_end")
        if remaining is None and limit is not None and used is not None:
            remaining = limit - used
        self.credits_remaining = remaining
        return {
            "remaining": remaining,
            "used": used,
            "limit": limit,
            "resets": locals().get("resets"),
            "last_call": None,
            "raw": data,
        }

    # -- listings ---------------------------------------------------------
    def get_sports(self, include_all: bool = False) -> list:
        # Nothing documents this endpoint as free, so it is counted. Better to
        # over-report local spend than to quietly overrun a 1,000/month tier.
        payload = self._call("v1/sports")
        # Their odds come wrapped as {"events": [...]}, so a listing is likely
        # wrapped under its own noun. Not added to the shared unwrapper: an
        # event block carries a "bookmakers" key that must not be unwrapped.
        if isinstance(payload, dict):
            for key in ("sports", "data", "results", "items"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        rows = []
        for item in _as_list(payload):
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

    def _cached_keys(self, sport: str) -> tuple:
        """Market keys learned on an earlier run, if they are still fresh."""
        if self.market_cache is None:
            return ()
        try:
            keys, _checked_at = self.market_cache.get_market_keys(sport, self.market_keys_ttl)
        except Exception:  # a cache miss must never break a poll
            log.debug("market key cache unreadable for %s", sport, exc_info=True)
            return ()
        return tuple(keys or ())

    def _remember_keys(self, sport: str, keys: Sequence[str]) -> None:
        self._game_market_cache[sport] = tuple(keys)
        if self.market_cache is None or not keys:
            return
        try:
            self.market_cache.save_market_keys(sport, {key: True for key in keys})
        except Exception:
            log.debug("could not persist market keys for %s", sport, exc_info=True)

    def _wants_all_game_markets(self) -> bool:
        return any(m.strip().lower() in ("all", "*") for m in self.featured_markets)

    def _game_markets(self, sport: str) -> tuple:
        """The market keys to request for a sport.

        "all" is not a market name — the API rejects it — so it is resolved to
        the set the API itself reports as valid, learned on the first rejection
        and remembered per sport.
        """
        if sport in self._game_market_cache:
            return self._game_market_cache[sport]
        cached = self._cached_keys(sport)
        if cached:
            self._game_market_cache[sport] = cached
            return cached
        if self._wants_all_game_markets():
            if not self._valid_markets:
                self._valid_markets = self._cached_keys(GLOBAL_MARKET_KEYS)
            if self._valid_markets:
                # Already learned from another sport's rejection, this run or a
                # previous one.
                return self._valid_markets
            # Deliberately send the literal "all" once: the API rejects it with
            # the full list of keys it will accept, which is a better source
            # than any list hard-coded here.
            return ("all",)
        return tuple(self.featured_markets)

    def _sport_odds(self, sport: str) -> Any:
        """Game markets. Without an explicit `markets` the API returns h2h only."""

        def request(markets: Sequence[str]) -> Any:
            params = {"markets": ",".join(markets), "oddsFormat": self.odds_format}
            if self.regions:
                params["regions"] = ",".join(self.regions)
            if self.bookmakers:
                params["bookmakers"] = ",".join(self.bookmakers)
            return self._call(f"v1/sports/{sport}/odds", params)

        markets = self._game_markets(sport)
        try:
            payload = request(markets)
        except HttpError as exc:
            valid = valid_markets_from_error(str(exc)) if exc.status == 400 else ()
            if not valid:
                raise
            if not self._valid_markets:
                self._valid_markets = tuple(
                    m for m in valid
                    if not m.startswith(("player_", "batter_", "pitcher_"))
                )
                self._remember_keys(GLOBAL_MARKET_KEYS, self._valid_markets)
            if self._wants_all_game_markets():
                wanted = tuple(m for m in valid if not m.startswith(("player_", "batter_", "pitcher_")))
            else:
                wanted = tuple(m for m in markets if m in valid) or CORE_GAME_MARKETS
            log.warning(
                "%s rejected the requested markets; retrying with the %d it accepts",
                sport,
                len(wanted),
            )
            try:
                payload = request(wanted)
            except HttpError:
                # Some listed keys belong to other sports; fall back to the core.
                log.warning("%s: falling back to %s", sport, ", ".join(CORE_GAME_MARKETS))
                wanted = CORE_GAME_MARKETS
                payload = request(wanted)
            self._remember_keys(sport, wanted)
        else:
            if markets != ("all",):
                # Accepted as sent: remember it, so the next run does not have
                # to relearn this sport through a rejection.
                self._remember_keys(sport, markets)
        return payload

    def _sport_props(self, sport: str) -> Any:
        """Player props, which arrive as flat rows rather than nested markets."""
        params = {"oddsFormat": self.odds_format}
        explicit = [m for m in self.prop_markets if m.strip().lower() not in ("all", "*")]
        if explicit:
            params["markets"] = ",".join(explicit)
        if self.bookmakers:
            params["bookmakers"] = ",".join(self.bookmakers)
        return self._call(f"v1/sports/{sport}/props", params)

    def prop_market_keys(self, sport: str) -> list:
        """The prop market keys this sport offers, from the reference endpoint."""
        payload = self._call(f"v1/sports/{sport}/props/markets")
        if isinstance(payload, dict):
            for key in ("markets", "data", "results", "items"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        rows = []
        for item in _as_list(payload):
            if isinstance(item, str):
                rows.append((item, item))
            elif isinstance(item, dict):
                key = _first(item, "key", "market", "market_key", "slug", "name", default=None)
                if key:
                    rows.append((str(key), _text(_first(item, "title", "name", default=key))))
        return sorted(set(rows))

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
