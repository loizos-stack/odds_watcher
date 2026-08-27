"""Choose the odds provider.

Both clients expose the same surface — events, odds, listings, and the
payload parsers — so everything downstream of here is provider-agnostic.
"""

from __future__ import annotations

from .config import Config
from .odds_api import OddsApiClient
from .parlayapi import ParlayApiClient
from .theoddsapi import TheOddsApiClient

PROVIDERS = ("odds-api-io", "the-odds-api", "parlay-api")


def _default_sport(config: Config) -> str:
    """The sport a client falls back to when a call names none.

    "all" is the watcher's instruction to expand the listing, not a key any
    provider accepts, so it must never reach one as a default.
    """
    if config.wants_all_sports or not config.sports:
        return ""
    return config.sports[0]


def build_client(config: Config, budget=None, market_cache=None):
    """Construct the client for ``ODDS_PROVIDER``."""
    if config.odds_provider == "parlay-api":
        return ParlayApiClient(
            config.odds_api_key,
            base_url=config.parlay_api_base_url,
            timeout=config.request_timeout_seconds,
            budget=budget,
            prop_markets=config.prop_markets,
            prop_sports=config.prop_sports,
            default_sport=_default_sport(config),
            odds_format=config.odds_format,
            regions=config.regions,
            featured_markets=config.featured_markets,
            bookmakers=config.bookmakers,
            market_cache=market_cache,
            market_keys_ttl=config.market_keys_ttl_seconds,
        )
    if config.odds_provider == "the-odds-api":
        return TheOddsApiClient(
            config.odds_api_key,
            base_url=config.the_odds_api_base_url,
            timeout=config.request_timeout_seconds,
            budget=budget,
            regions=config.regions,
            featured_markets=config.featured_markets,
            prop_markets=config.prop_markets,
            odds_format="decimal",
            default_sport=_default_sport(config),
            market_cache=market_cache,
            market_keys_ttl=config.market_keys_ttl_seconds,
        )
    return OddsApiClient(
        config.odds_api_key,
        base_url=config.api_base_url,
        timeout=config.request_timeout_seconds,
        budget=budget,
    )
