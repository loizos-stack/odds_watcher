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


DEFAULT_BOOKMAKERS = ("bet365", "betano")

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
    sports: tuple[str, ...] = ("football",)
    leagues: tuple[str, ...] = ()
    markets: tuple[str, ...] = ()  # empty == every market the API returns

    # --- when an alert may fire -----------------------------------------
    # The alert window is [window_end_seconds, window_start_seconds] before
    # kick-off; the defaults mean "0 to 10 minutes before game time".
    window_start_seconds: int = 600
    window_end_seconds: int = 0
    # How long before kick-off we start recording prices, so that a baseline
    # exists by the time an event enters the alert window. This only needs to
    # clear the window by a couple of polls — a longer lead spends the request
    # budget on prices that are thrown away.
    baseline_lead_seconds: int = 900
    min_drop_pct: float = 5.0
    # Ignore prices below this (a 1.02 favourite drifts in meaningless %).
    min_odds: float = 1.05

    # --- polling / budget ------------------------------------------------
    poll_interval_seconds: int = 60
    idle_poll_interval_seconds: int = 300
    events_refresh_seconds: int = 900
    max_requests_per_hour: int = 90
    max_requests_per_day: int = 450

    # --- plumbing --------------------------------------------------------
    api_base_url: str = "https://api2.odds-api.io/v3"
    request_timeout_seconds: int = 20
    db_path: Path = field(default=Path("odds_watcher.db"))
    log_level: str = "INFO"
    dry_run: bool = False

    @property
    def alert_window_label(self) -> str:
        return f"{self.window_end_seconds // 60}-{self.window_start_seconds // 60} min before kick-off"

    @classmethod
    def from_env(
        cls,
        env: Optional[dict] = None,
        *,
        required: Sequence[str] = ALL_CREDENTIALS,
    ) -> "Config":
        """Build a config from the environment.

        ``required`` lets a command ask for only the credentials it actually
        uses, so `chat-id` can run before TELEGRAM_CHAT_ID is known.
        """
        env = dict(os.environ if env is None else env)

        missing = [name for name in required if not env.get(name)]
        if missing:
            hint = "Copy .env.example to .env and fill it in."
            if missing == ["TELEGRAM_CHAT_ID"]:
                hint = "Message your bot in Telegram, then run `chat-id` to discover it."
            raise ConfigError(
                "Missing required environment variable(s): " + ", ".join(missing) + ". " + hint
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
        baseline_lead = _get_int(env, "BASELINE_LEAD_SECONDS", 900, minimum=0)
        poll_interval = _get_int(env, "POLL_INTERVAL_SECONDS", 60, minimum=10)
        # The lead has to clear the window by at least two polls, or a fixture
        # can slip from "not tracked yet" straight into the window with no
        # pre-window price — and a line with no baseline can never alert.
        minimum_lead = window_start + 2 * poll_interval
        if baseline_lead < minimum_lead:
            raise ConfigError(
                f"BASELINE_LEAD_SECONDS ({baseline_lead}) must be at least "
                f"WINDOW_START_SECONDS + 2 x POLL_INTERVAL_SECONDS ({minimum_lead}), "
                "otherwise fixtures can enter the alert window with no baseline price "
                "and will never alert"
            )

        return cls(
            odds_api_key=env.get("ODDS_API_KEY", "").strip(),
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=env.get("TELEGRAM_CHAT_ID", "").strip(),
            bookmakers=bookmakers,
            sports=_csv(env.get("SPORTS", "football")) or ("football",),
            leagues=_csv(env.get("LEAGUES", "")),
            markets=_csv(env.get("MARKETS", "")),
            window_start_seconds=window_start,
            window_end_seconds=window_end,
            baseline_lead_seconds=baseline_lead,
            min_drop_pct=_get_float(env, "MIN_DROP_PCT", 5.0, minimum=0.1),
            min_odds=_get_float(env, "MIN_ODDS", 1.05, minimum=1.0),
            poll_interval_seconds=poll_interval,
            idle_poll_interval_seconds=_get_int(env, "IDLE_POLL_INTERVAL_SECONDS", 300, minimum=10),
            events_refresh_seconds=_get_int(env, "EVENTS_REFRESH_SECONDS", 900, minimum=60),
            max_requests_per_hour=_get_int(env, "MAX_REQUESTS_PER_HOUR", 90, minimum=1),
            max_requests_per_day=_get_int(env, "MAX_REQUESTS_PER_DAY", 450, minimum=1),
            api_base_url=env.get("ODDS_API_BASE_URL", "https://api2.odds-api.io/v3").rstrip("/"),
            request_timeout_seconds=_get_int(env, "REQUEST_TIMEOUT_SECONDS", 20, minimum=1),
            db_path=Path(env.get("DB_PATH", "odds_watcher.db")).expanduser(),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            dry_run=_get_bool(env, "DRY_RUN"),
        )
