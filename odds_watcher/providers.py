"""Choose the odds provider.

Both clients expose the same surface — events, odds, listings, and the
payload parsers — so everything downstream of here is provider-agnostic.
"""

from __future__ import annotations

from .config import Config
from .odds_api import OddsApiClient
from .theoddsapi import TheOddsApiClient

PROVIDERS = ("odds-api-io", "the-odds-api")


def build_client(config: Config, budget=None):
    """Construct the client for ``ODDS_PROVIDER``."""
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
            default_sport=config.sports[0] if config.sports else "",
        )
    return OddsApiClient(
        config.odds_api_key,
        base_url=config.api_base_url,
        timeout=config.request_timeout_seconds,
        budget=budget,
    )
