"""Configuration loading for the odds watcher.

Everything is driven by environment variables so the bot can run identically
from a shell, a systemd unit, cron or a container.  A `.env` file sitting next
to the project root is loaded automatically when present (no third-party
dependency: the parser below handles the simple `KEY=value` form).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo


DEFAULT_BOOKMAKERS = ("Bet365", "DraftKings")

# Credentials every normal run needs. Individual commands narrow this: the
# `chat-id` command exists precisely because the chat id is not known yet.
ALL_CREDENTIALS = ("ODDS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


class ConfigError(RuntimeError):
    """Raised when the environment is missing or contains invalid settings."""


def load_dotenv(path: Path) -> None:
    """Populate os.environ from a `.env` file. Existing vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _get_int(env: dict, name: str, default: int, *, minimum: Optional[int] = None) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_float(env: dict, name: str, default: float, *, minimum: Optional[float] = None) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_bool(env: dict, name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Everything the watcher needs to run."""

    # --- credentials -----------------------------------------------------
    odds_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str

    # --- what to watch ---------------------------------------------------
    bookmakers: tuple[str, ...] = DEFAULT_BOOKMAKERS
    sports: tuple[str, ...] = ("baseball",)
    leagues: tuple[str, ...] = ()
    markets: tuple[str, ...] = ()  # empty == every market the API returns
    outcomes: tuple[str, ...] = ()  # empty == every outcome, e.g. ("over", "under")

    # --- when an alert may fire -----------------------------------------
    # The alert window is [window_end_seconds, window_start_seconds] before
    # kick-off; the defaults mean "0 to 10 minutes before game time".
    window_start_seconds: int = 600
    window_end_seconds: int = 0
    # How long before kick-off we start recording prices, so that a baseline
    # exists by the time an event enters the alert window. This only needs to
    # clear the window by a couple of polls — a longer lead spends the request
    # budget on prices that are thrown away.
    baseline_lead_seconds: int = 1200
    min_drop_pct: float = 2.0
    # How the reference price is chosen:
    #   "window-entry" - the last price before the alert window opens; only
    #                    movement inside the window can alert.
    #   "first-seen"   - the first price recorded for the line; any drop from
    #                    the start of tracking until kick-off can alert.
    #   "last-seen"    - the previous recorded price; every poll is compared
    #                    with the one before it, inside the alert window.
    baseline_mode: str = "window-entry"
    # Which scale MIN_DROP_PCT is measured on. Bettors quote a move as a
    # percentage of the American price (-110 to -121 is "10%"); the same
    # move is 4.33% in decimal, so the choice changes what fires by 2x.
    drop_metric: str = "decimal"
    # Once a line has signalled, the interest is in whether it KEEPS moving,
    # so any further shortening is worth saying. 0.0 means "any drop at all".
    follow_up_drop_pct: float = 0.0
    # IANA zone for timestamps in messages. Kick-off times are the thing being
    # reasoned about, and reading them in another zone invites a real mistake.
    display_timezone: str = "UTC"
    # Batch alerts into one per-player summary every N seconds instead of
    # sending each drop as it happens. 0 = send immediately (the default).
    digest_interval_seconds: int = 0
    # Send the hourly digest to a different Telegram chat. Empty = same chat as
    # the per-drop alerts.
    digest_chat_id: str = ""
    # Ignore prices below this (a 1.02 favourite drifts in meaningless %).
    min_odds: float = 1.05
    # Ceiling on messages from one poll. With every market and player prop
    # enabled a single poll can find hundreds of drops; the biggest are sent
    # and the rest are logged, so a busy minute cannot flood the chat or trip
    # Telegram's own rate limits.
    max_alerts_per_poll: int = 20

    # --- polling / budget ------------------------------------------------
    poll_interval_seconds: int = 60
    idle_poll_interval_seconds: int = 300
    events_refresh_seconds: int = 900
    max_requests_per_hour: int = 90
    max_requests_per_day: int = 450

    # --- provider --------------------------------------------------------
    # "odds-api-io" or "the-odds-api". The two differ in how odds are fetched
    # and metered, but present the same events and prices downstream.
    odds_provider: str = "odds-api-io"
    # The Odds API only: regions to query, and its market keys. Usage there is
    # metered as markets x regions per call, and props are per fixture.
    regions: tuple = ("us",)
    featured_markets: tuple = ("h2h", "spreads", "totals")
    prop_markets: tuple = ()
    # Which sports get player props. Empty means all of them. Props cost a
    # second request per sport per poll, so on a wide SPORTS list this is the
    # setting that decides whether props are affordable at all.
    prop_sports: tuple[str, ...] = ()
    # How long a discovered market-key set stays usable before it is re-probed.
    market_keys_ttl_seconds: int = 86400
    the_odds_api_base_url: str = "https://api.the-odds-api.com/v4"
    parlay_api_base_url: str = "https://parlay-api.com"
    # How the provider quotes prices: "american", "decimal" or "auto".
    odds_format: str = "american"

    # --- plumbing --------------------------------------------------------
    # Fetch odds one fixture at a time instead of batching. Player props are
    # documented as being available per event, so a batched request may return
    # only game markets. Costs one request per fixture per poll.
    per_event_odds: bool = False
    api_base_url: str = "https://api2.odds-api.io/v3"
    request_timeout_seconds: int = 20
    db_path: Path = field(default=Path("odds_watcher.db"))
    log_level: str = "INFO"
    dry_run: bool = False

    @property
    def wants_all_sports(self) -> bool:
        """True when SPORTS asks for every competition the provider lists."""
        return any(entry.strip().lower() in ("all", "*") for entry in self.sports)

    @property
    def wants_all_markets(self) -> bool:
        """True when PROP_MARKETS asks for everything the sport offers."""
        return any(entry.strip().lower() in ("all", "*") for entry in self.prop_markets)

    @property
    def alert_window_label(self) -> str:
        if self.baseline_mode == "last-seen":
            return (
                f"{self.window_end_seconds // 60}-{self.window_start_seconds // 60} "
                "min before kick-off (vs the previous price)"
            )
        if self.baseline_mode == "first-seen":
            return (
                f"{self.window_end_seconds // 60}-{self.baseline_lead_seconds // 60} "
                "min before kick-off (from first price seen)"
            )
        return f"{self.window_end_seconds // 60}-{self.window_start_seconds // 60} min before kick-off"

    @classmethod
    def from_env(
        cls,
        env: Optional[dict] = None,
        *,
        required: Sequence[str] = ALL_CREDENTIALS,
        env_file: Optional[Path] = None,
    ) -> "Config":
        """Build a config from the environment.

        ``required`` lets a command ask for only the credentials it actually
        uses, so `chat-id` can run before TELEGRAM_CHAT_ID is known.
        """
        env = dict(os.environ if env is None else env)

        missing = [name for name in required if not env.get(name)]
        if missing:
            # "Copy .env.example to .env" is the wrong advice when a .env is
            # already there with the value blank -- it invites overwriting the
            # file again, which is how the value went missing the first time.
            hint = "Set them in your .env."
            if env_file is not None and Path(env_file).is_file():
                hint = f"They are present but empty in {env_file}; edit it in place."
                backup = Path(str(env_file) + ".bak")
                if backup.is_file():
                    hint += f" The previous version is {backup}."
            elif env_file is not None:
                hint = (f"There is no {env_file}. Start from a preset: "
                        "`odds_watcher preset --source .env.example`.")
            if missing == ["TELEGRAM_CHAT_ID"]:
                hint = "Message your bot in Telegram, then run `chat-id` to discover it."
            raise ConfigError(
                "Missing required environment variable(s): " + ", ".join(missing) + ". " + hint
            )

        provider = env.get("ODDS_PROVIDER", "odds-api-io").strip().lower()
        if provider not in ("odds-api-io", "the-odds-api", "parlay-api"):
            raise ConfigError(
                "ODDS_PROVIDER must be one of 'odds-api-io', 'the-odds-api', "
                f"'parlay-api', got {provider!r}"
            )

        follow_up = _get_float(env, "FOLLOW_UP_DROP_PCT", 0.0, minimum=0.0)
        display_timezone = env.get("DISPLAY_TIMEZONE", "UTC").strip() or "UTC"
        try:
            ZoneInfo(display_timezone)
        except Exception as exc:  # unknown zone, or no tzdata on the host
            raise ConfigError(
                f"DISPLAY_TIMEZONE={display_timezone!r} is not a zone this "
                f"system knows ({exc}). Use an IANA name such as Asia/Nicosia, "
                "or UTC."
            ) from exc
        drop_metric = env.get("DROP_METRIC", "decimal").strip().lower()
        if drop_metric not in ("decimal", "american"):
            raise ConfigError(
                "DROP_METRIC must be 'decimal' or 'american', got "
                f"{drop_metric!r}"
            )
        baseline_mode = env.get("BASELINE_MODE", "window-entry").strip().lower()
        if baseline_mode not in ("window-entry", "first-seen", "last-seen"):
            raise ConfigError(
                "BASELINE_MODE must be 'window-entry', 'first-seen' or "
                f"'last-seen', got {baseline_mode!r}"
            )

        bookmakers = _csv(env.get("BOOKMAKERS", ",".join(DEFAULT_BOOKMAKERS)))
        if not bookmakers:
            raise ConfigError("BOOKMAKERS must list at least one bookmaker")

        window_start = _get_int(env, "WINDOW_START_SECONDS", 600, minimum=0)
        window_end = _get_int(env, "WINDOW_END_SECONDS", 0, minimum=0)
        if window_end >= window_start:
            raise ConfigError(
                "WINDOW_END_SECONDS must be smaller than WINDOW_START_SECONDS "
                f"(got end={window_end}, start={window_start})"
            )
        poll_interval = _get_int(env, "POLL_INTERVAL_SECONDS", 60, minimum=10)
        # The lead has to clear the window by a few polls. Deriving the default
        # from the poll interval means a slower poll does not silently leave
        # fixtures without a baseline — or force the arithmetic onto the user.
        default_lead = max(1200, window_start + 3 * poll_interval)
        baseline_lead = _get_int(env, "BASELINE_LEAD_SECONDS", default_lead, minimum=0)
        # The lead has to clear the window by at least two polls, or a fixture
        # can slip from "not tracked yet" straight into the window with no
        # pre-window price — and a line with no baseline can never alert.
        minimum_lead = window_start + 2 * poll_interval
        if baseline_lead < minimum_lead:
            raise ConfigError(
                f"BASELINE_LEAD_SECONDS ({baseline_lead}) must be at least "
                f"WINDOW_START_SECONDS + 2 x POLL_INTERVAL_SECONDS ({minimum_lead}), "
                "otherwise fixtures can enter the alert window with no baseline price "
                "and will never alert.\n"
                f"  Either set BASELINE_LEAD_SECONDS={default_lead}, or lower "
                f"POLL_INTERVAL_SECONDS to {max((baseline_lead - window_start) // 2, 10)} "
                "or less. Removing BASELINE_LEAD_SECONDS from .env derives it for you."
            )

        return cls(
            odds_api_key=env.get("ODDS_API_KEY", "").strip(),
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID", "").strip(),
            bookmakers=bookmakers,
            sports=_csv(env.get("SPORTS", "baseball")) or ("baseball",),
            leagues=_csv(env.get("LEAGUES", "")),
            markets=_csv(env.get("MARKETS", "")),
            outcomes=_csv(env.get("OUTCOMES", "")),
            window_start_seconds=window_start,
            window_end_seconds=window_end,
            baseline_lead_seconds=baseline_lead,
            min_drop_pct=_get_float(env, "MIN_DROP_PCT", 2.0, minimum=0.1),
            baseline_mode=baseline_mode,
            drop_metric=drop_metric,
            follow_up_drop_pct=follow_up,
            display_timezone=display_timezone,
            digest_interval_seconds=_get_int(env, "DIGEST_INTERVAL_SECONDS", 0, minimum=0),
            digest_chat_id=env.get("TELEGRAM_DIGEST_CHAT_ID", "").strip(),
            min_odds=_get_float(env, "MIN_ODDS", 1.05, minimum=1.0),
            max_alerts_per_poll=_get_int(env, "MAX_ALERTS_PER_POLL", 20, minimum=1),
            poll_interval_seconds=poll_interval,
            idle_poll_interval_seconds=_get_int(env, "IDLE_POLL_INTERVAL_SECONDS", 300, minimum=10),
            events_refresh_seconds=_get_int(env, "EVENTS_REFRESH_SECONDS", 900, minimum=60),
            max_requests_per_hour=_get_int(env, "MAX_REQUESTS_PER_HOUR", 90, minimum=1),
            max_requests_per_day=_get_int(env, "MAX_REQUESTS_PER_DAY", 450, minimum=1),
            per_event_odds=_get_bool(env, "PER_EVENT_ODDS"),
            odds_provider=provider,
            regions=_csv(env.get("REGIONS", "us")) or ("us",),
            # The odds endpoint requires at least one market; an empty setting
            # would silently ask for none and be billed as one credit anyway.
            featured_markets=(
                # An ABSENT key gets the game-market default; an explicitly
                # EMPTY key means "no game markets" (props-only) and must be
                # honoured, not silently turned back into the default.
                _csv(env["FEATURED_MARKETS"])
                if "FEATURED_MARKETS" in env
                else ("h2h", "spreads", "totals")
            ),
            prop_markets=_csv(env.get("PROP_MARKETS", "")),
            prop_sports=_csv(env.get("PROP_SPORTS", "")),
            market_keys_ttl_seconds=_get_int(env, "MARKET_KEYS_TTL_SECONDS", 86400, minimum=600),
            odds_format=env.get("ODDS_FORMAT", "american").strip().lower(),
            parlay_api_base_url=env.get(
                "PARLAY_API_BASE_URL", "https://parlay-api.com"
            ).rstrip("/"),
            the_odds_api_base_url=env.get(
                "THE_ODDS_API_BASE_URL", "https://api.the-odds-api.com/v4"
            ).rstrip("/"),
            api_base_url=env.get("ODDS_API_BASE_URL", "https://api2.odds-api.io/v3").rstrip("/"),
            request_timeout_seconds=_get_int(env, "REQUEST_TIMEOUT_SECONDS", 20, minimum=1),
            db_path=Path(env.get("DB_PATH", "odds_watcher.db")).expanduser(),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            dry_run=_get_bool(env, "DRY_RUN"),
        )
