"""Client for The Odds API v4 (https://api.the-odds-api.com/v4).

Shaped to the same interface as the odds-api.io client, so the detector,
store, watcher and Telegram layer are unchanged.

Two things about this provider drive the design:

* **Featured markets and player props come from different endpoints.**
  ``/sports/{sport}/odds`` serves h2h, spreads and totals for the whole slate
  in one call. Everything else — player props included — is only available
  from ``/sports/{sport}/events/{eventId}/odds``, one fixture at a time.

* **Usage is metered as ``markets x regions`` per call.** A featured-market
  poll covering fifteen games costs 3 credits; the same poll fetching three
  prop markets per game costs 45. The client tracks the remaining allowance
  from the ``x-requests-remaining`` response header so the burn is visible
  rather than discovered when the account dries up.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from .http import HttpError, build_url, redact, request_json_with_headers
from .odds_api import BudgetExceeded, Event, Quote
from .util import parse_timestamp

log = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
FEATURED_MARKETS = ("h2h", "spreads", "totals")


class TheOddsApiClient:
    """The Odds API v4, exposing the interface the watcher expects."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: int = 20,
        budget=None,
        regions: Sequence[str] = ("us",),
        featured_markets: Sequence[str] = FEATURED_MARKETS,
        prop_markets: Sequence[str] = (),
        odds_format: str = "decimal",
        default_sport: str = "",
        market_cache=None,
        market_keys_ttl: int = 86400,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.budget = budget
        self.regions = tuple(regions)
        self.featured_markets = tuple(featured_markets)
        self.prop_markets = tuple(prop_markets)
        self.odds_format = odds_format
        # Odds calls are scoped by sport key here, unlike odds-api.io where an
        # event id is enough.
        self.default_sport = default_sport
        # Store used to remember which market keys this account can request.
        self.market_cache = market_cache
        self.market_keys_ttl = market_keys_ttl
        self._resolved_props: dict = {}
        # Populated from response headers after the first metered call.
        self.credits_remaining: Optional[int] = None
        self.credits_used: Optional[int] = None
        self.last_call_cost: Optional[int] = None
        # Batching is inherent here: the featured endpoint covers a whole
        # sport. Kept for interface parity with the odds-api.io client.
        self.supports_multi = True

    # -- plumbing ---------------------------------------------------------
    def _call(self, path: str, params: Optional[dict] = None, *, metered: bool = True,
              cost: int = 1) -> Any:
        if metered and self.budget is not None and not self.budget.try_consume(cost):
            raise BudgetExceeded(
                f"local budget cannot cover {cost} credit(s) for {path}"
            )
        query = {"api_key": self.api_key, **(params or {})}
        url = build_url(self.base_url, path, query)
        log.debug("GET %s", redact(url))
        data, headers = request_json_with_headers(url, timeout=self.timeout)
        self._read_quota(headers)
        return data

    def _read_quota(self, headers: dict) -> None:
        for header, attribute in (
            ("x-requests-remaining", "credits_remaining"),
            ("x-requests-used", "credits_used"),
            ("x-requests-last", "last_call_cost"),
        ):
            value = headers.get(header)
            if value is None:
                continue
            try:
                setattr(self, attribute, int(float(value)))
            except (TypeError, ValueError):
                continue
        if self.credits_remaining is not None and self.credits_remaining < 500:
            log.warning("The Odds API credits running low: %s remaining", self.credits_remaining)

    def quota_cost(self, markets: Sequence[str]) -> int:
        """Credits one call with these markets costs: markets x regions."""
        return max(len(markets), 1) * max(len(self.regions), 1)

    # -- listings ---------------------------------------------------------
    def get_sports(self, include_all: bool = False) -> list:
        """Sports, which here are the competitions themselves (baseball_mlb).

        Only in-season ones are returned unless ``include_all`` is set. This
        endpoint does not consume credits either way.
        """
        payload = self._call("sports", {"all": "true"} if include_all else None, metered=False) or []
        rows = []
        for item in payload:
            if isinstance(item, dict) and item.get("key"):
                title = item.get("title") or item["key"]
                group = item.get("group")
                rows.append((str(item["key"]), f"{title}" + (f" ({group})" if group else "")))
        return sorted(set(rows))

    def get_leagues(self, sport: str) -> list:
        """The Odds API has no separate league concept — sports keys are it."""
        raise UnsupportedByProvider(
            "The Odds API has no separate league list: a sport key such as "
            "'baseball_mlb' already identifies the competition. Use `sports`."
        )

    def get_bookmakers(self) -> list:
        """Bookmaker keys, discovered from a sample of live odds.

        There is no bookmakers endpoint here; the keys appear inside odds
        responses. Which books appear depends entirely on REGIONS — bet365 is
        in uk/eu, DraftKings and FanDuel in us — so this samples the sport that
        is actually configured, and costs one featured call.
        """
        sport = self.default_sport or "upcoming"
        rows: set = set()
        for block in self._featured_odds(sport) or []:
            for book in block.get("bookmakers", []) or []:
                if isinstance(book, dict) and book.get("key"):
                    rows.add((str(book["key"]), str(book.get("title") or book["key"])))
        return sorted(rows)

    def get_selected_bookmakers(self) -> list:
        """No account-level selection exists; books are chosen per request."""
        return [b.lower() for b in ()]

    def select_bookmakers(self, bookmakers: Sequence[str]) -> Any:
        raise UnsupportedByProvider(
            "The Odds API selects bookmakers per request (via BOOKMAKERS/regions), "
            "not on the account. Nothing to do."
        )

    # -- events and odds --------------------------------------------------
    def get_events(self, sport: str, *, league: Optional[str] = None, limit: Optional[int] = None) -> list:
        """Upcoming fixtures. This endpoint does not consume credits."""
        payload = self._call(f"sports/{sport}/events", metered=False) or []
        events = [parse_event(raw) for raw in payload if isinstance(raw, dict)]
        return [event for event in events if event is not None]

    def _featured_odds(self, sport: str, bookmakers: Sequence[str] = ()) -> list:
        params = {
            "regions": ",".join(self.regions),
            "markets": ",".join(self.featured_markets),
            "oddsFormat": self.odds_format,
            "dateFormat": "iso",
        }
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        payload = self._call(f"sports/{sport}/odds", params, cost=self.quota_cost(self.featured_markets))
        return payload if isinstance(payload, list) else []

    def _event_odds(self, sport: str, event_id: str, markets: Sequence[str],
                    bookmakers: Sequence[str] = ()) -> Any:
        params = {
            "regions": ",".join(self.regions),
            "markets": ",".join(markets),
            "oddsFormat": self.odds_format,
            "dateFormat": "iso",
        }
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        return self._call(
            f"sports/{sport}/events/{event_id}/odds", params, cost=self.quota_cost(markets)
        )

    def get_odds_payloads(self, event_ids: Sequence[str], bookmakers: Sequence[str],
                          *, sport: str = "", fallback_limit: int = 5) -> list:
        """Raw blocks for the given fixtures, featured markets plus any props."""
        sport = sport or self.default_sport
        if not sport:
            raise ValueError("The Odds API needs a sport key to fetch odds")
        wanted = set(event_ids)
        blocks = [b for b in self._featured_odds(sport, bookmakers) if b.get("id") in wanted]
        if self.prop_markets:
            ids = list(event_ids)[:fallback_limit]
            markets = self.resolve_prop_markets(ids[0], bookmakers, sport) if ids else ()
            for event_id in ids:
                if not markets:
                    break
                try:
                    extra = self._event_odds(sport, event_id, markets, bookmakers)
                except HttpError as exc:
                    log.warning("prop markets unavailable for %s: %s", event_id, exc)
                    continue
                if isinstance(extra, dict):
                    blocks.append(extra)
        return blocks


    @property
    def wants_all_markets(self) -> bool:
        return any(str(m).strip().lower() in ("all", "*") for m in self.prop_markets)

    def resolve_prop_markets(
        self, event_id: str, bookmakers: Sequence[str] = (), sport: str = ""
    ) -> tuple:
        """The prop keys to request, expanding "all" against the learned set.

        Asking for every documented key on every poll would fail as soon as one
        of them is not recognised, so the usable set is discovered once and
        cached; it is re-probed when the cache expires.
        """
        if not self.wants_all_markets:
            return tuple(self.prop_markets)
        sport = sport or self.default_sport
        if sport in self._resolved_props:
            return self._resolved_props[sport]

        from .market_keys import candidates
        if self.market_cache is not None:
            cached, _checked = self.market_cache.get_market_keys(sport, self.market_keys_ttl)
            if cached is not None:
                self._resolved_props[sport] = tuple(cached)
                log.info("using %d cached market key(s) for %s", len(cached), sport)
                return self._resolved_props[sport]

        keys = candidates(sport)
        if not keys:
            log.warning("no candidate market keys catalogued for %s", sport)
            self._resolved_props[sport] = ()
            return self._resolved_props[sport]

        log.info(
            "probing %d market key(s) for %s (about %d credits, once per %dh)",
            len(keys),
            sport,
            len(keys) * max(len(self.regions), 1),
            self.market_keys_ttl // 3600,
        )
        result = self.discover_markets(event_id, keys, bookmakers, sport=sport)
        usable = sorted(set(result["available"]) | set(result["empty"]))
        if self.market_cache is not None:
            self.market_cache.save_market_keys(
                sport,
                {**{k: True for k in usable}, **{k: False for k in result["rejected"]}},
            )
        log.info(
            "market keys for %s: %d usable, %d rejected",
            sport,
            len(usable),
            len(result["rejected"]),
        )
        self._resolved_props[sport] = tuple(usable)
        return self._resolved_props[sport]

    def discover_markets(self, event_id: str, candidates: Sequence[str],
                         bookmakers: Sequence[str] = (), chunk: int = 8,
                         sport: str = "") -> dict:
        """Ask for candidate market keys and report which ones answer.

        Returns ``{"available": {...}, "empty": [...], "rejected": [...]}``.
        A key the API refuses fails the whole request, so a rejected chunk is
        retried key by key rather than being written off wholesale.
        """
        sport = sport or self.default_sport
        available: dict = {}
        empty: list = []
        rejected: list = []

        def attempt(keys: Sequence[str]) -> bool:
            try:
                payload = self._event_odds(sport, event_id, keys, bookmakers)
            except HttpError as exc:
                if exc.status in (400, 422, 404):
                    return False
                raise
            found = market_catalogue(payload)
            for key in keys:
                if key in found:
                    available[key] = found[key]
                else:
                    empty.append(key)
            return True

        for start in range(0, len(candidates), chunk):
            group = list(candidates[start : start + chunk])
            if attempt(group):
                continue
            # One bad key spoils the request, so isolate them.
            for key in group:
                if not attempt([key]):
                    rejected.append(key)

        return {"available": available, "empty": empty, "rejected": rejected}

    # -- interface parity with the odds-api.io client ---------------------
    def get_multi_odds(
        self, event_ids: Sequence[str], bookmakers: Sequence[str], *, sport: str = ""
    ) -> list:
        return parse_quotes(
            self.get_odds_payloads(
                event_ids, bookmakers, sport=sport, fallback_limit=len(event_ids)
            )
        )

    def get_event_odds(self, event_id: str, bookmakers: Sequence[str], *, sport: str = "") -> list:
        return parse_quotes(
            self.get_event_odds_raw(event_id, bookmakers, sport=sport), default_event_id=event_id
        )

    def get_event_odds_raw(self, event_id: str, bookmakers: Sequence[str], *, sport: str = "") -> Any:
        sport = sport or self.default_sport
        markets = tuple(self.featured_markets) + self.resolve_prop_markets(
            event_id, bookmakers, sport
        )
        return self._event_odds(sport, event_id, markets, bookmakers)

    def get_multi_odds_raw(
        self, event_ids: Sequence[str], bookmakers: Sequence[str], *, sport: str = ""
    ) -> Any:
        return self.get_odds_payloads(
            event_ids, bookmakers, sport=sport, fallback_limit=len(event_ids)
        )

    @staticmethod
    def parse_quotes(payload: Any, *, default_event_id: Optional[str] = None) -> list:
        return parse_quotes(payload, default_event_id=default_event_id)

    @staticmethod
    def market_catalogue(payload: Any) -> dict:
        return market_catalogue(payload)


class UnsupportedByProvider(RuntimeError):
    """The selected provider has no equivalent of the requested operation."""


def parse_event(raw: dict) -> Optional[Event]:
    """One fixture from the v4 events/odds payload."""
    event_id = raw.get("id")
    start_ts = parse_timestamp(raw.get("commence_time"))
    if not event_id or start_ts is None:
        return None
    return Event(
        id=str(event_id),
        start_ts=start_ts,
        home=str(raw.get("home_team") or "Home"),
        away=str(raw.get("away_team") or "Away"),
        sport=str(raw.get("sport_title") or raw.get("sport_key") or ""),
        sport_key=str(raw.get("sport_key") or ""),
        league=str(raw.get("sport_title") or ""),
        league_slug=str(raw.get("sport_key") or ""),
    )


def _outcome_name(outcome: dict) -> str:
    """Readable selection: props carry the player in `description`."""
    name = str(outcome.get("name") or "").strip()
    description = str(outcome.get("description") or "").strip()
    if description and name:
        return f"{description} {name}"
    return description or name


def parse_quotes(payload: Any, *, default_event_id: Optional[str] = None) -> list:
    """Flatten a v4 odds payload into :class:`Quote` objects."""
    blocks = payload if isinstance(payload, list) else [payload]
    quotes: list = []
    seen: set = set()
    for block in blocks:
        if not isinstance(block, dict):
            continue
        event_id = str(block.get("id") or default_event_id or "")
        if not event_id:
            continue
        for book in block.get("bookmakers") or []:
            if not isinstance(book, dict):
                continue
            book_key = str(book.get("key") or book.get("title") or "").strip().lower()
            if not book_key:
                continue
            book_ts = parse_timestamp(book.get("last_update"))
            for market in book.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                market_key = str(market.get("key") or "").strip()
                market_ts = parse_timestamp(market.get("last_update")) or book_ts
                for outcome in market.get("outcomes") or []:
                    if not isinstance(outcome, dict):
                        continue
                    try:
                        price = float(outcome.get("price"))
                    except (TypeError, ValueError):
                        continue
                    if price <= 0:
                        continue
                    point = outcome.get("point")
                    quote = Quote(
                        event_id=event_id,
                        bookmaker=book_key,
                        market=market_key,
                        line="" if point in (None, "") else str(point),
                        outcome=_outcome_name(outcome),
                        odds=price,
                        updated_ts=market_ts,
                    )
                    if not quote.outcome or quote.key in seen:
                        continue
                    seen.add(quote.key)
                    quotes.append(quote)
    return quotes


def market_catalogue(payload: Any) -> dict:
    """``{market key: {bookmaker: price count}}`` for a v4 payload."""
    catalogue: dict = {}
    for quote in parse_quotes(payload):
        entry = catalogue.setdefault(quote.market, {})
        entry[quote.bookmaker] = entry.get(quote.bookmaker, 0) + 1
    return catalogue
