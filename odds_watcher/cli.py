"""Command line entry point.

    python -m odds_watcher run                # daemon (default)
    python -m odds_watcher once               # single poll, for cron
    python -m odds_watcher check              # verify credentials end to end
    python -m odds_watcher select-bookmakers  # bind the account to your two books
    python -m odds_watcher chat-id            # discover your Telegram chat id
    python -m odds_watcher status             # budget + tracked lines
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .config import ALL_CREDENTIALS, Config, ConfigError, load_dotenv
from .detector import Alert
from .http import TransportError
from .odds_api import BudgetExceeded
from .providers import build_client
from .theoddsapi import UnsupportedByProvider
from .store import RequestBudget, Store
from .telegram import TelegramClient
from .util import format_clock, format_countdown, now_ts
from .watcher import Watcher

log = logging.getLogger("odds_watcher")


# Each command only demands the credentials it actually uses.
REQUIRED_CREDENTIALS = {
    "chat-id": ("TELEGRAM_BOT_TOKEN",),
    "bookmakers": ("ODDS_API_KEY",),
    "sports": ("ODDS_API_KEY",),
    "leagues": ("ODDS_API_KEY",),
    "probe": ("ODDS_API_KEY",),
    "markets": ("ODDS_API_KEY",),
    "coverage": ("ODDS_API_KEY",),
    "props": ("ODDS_API_KEY",),
    "usage": ("ODDS_API_KEY",),
    "select-bookmakers": ("ODDS_API_KEY",),
    "status": (),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odds_watcher",
        description="Telegram alerts when a bookmaker's odds drop just before kick-off.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "once", "check", "select-bookmakers", "bookmakers", "sports", "leagues", "markets", "props", "probe", "coverage", "usage", "chat-id", "status"],
    )
    parser.add_argument(
        "--sport",
        default=None,
        help="override SPORTS for this command, e.g. --sport baseball (discovery commands)",
    )
    parser.add_argument(
        "--search",
        default=None,
        help="filter the `bookmakers` / `leagues` listing, e.g. --search premier",
    )
    parser.add_argument("--env-file", default=".env", help="path to the .env file (default: .env)")
    parser.add_argument("--log-level", default=None, help="override LOG_LEVEL")
    parser.add_argument(
        "--discover",
        action="store_true",
        help="markets: probe documented prop keys to see which your books return",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="include_all",
        help="include out-of-season competitions in the `sports` listing",
    )
    parser.add_argument(
        "--check-balance",
        action="store_true",
        help="usage: query the provider's remaining allowance (costs one request)",
    )
    parser.add_argument(
        "--reset-budget",
        action="store_true",
        help="clear the watcher's local request/credit tally (status)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="detect and log drops without sending Telegram messages",
    )
    return parser


def force_utf8_output() -> None:
    """Make stdout/stderr UTF-8 regardless of the platform's code page.

    On Windows the console is fine, but as soon as output is redirected to a
    file or a pipe Python falls back to the locale encoding (cp1252), and the
    ✓/📉 characters in this CLI raise UnicodeEncodeError. Reconfiguring with
    errors="replace" keeps output readable everywhere instead of crashing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # already detached, or not a text stream
            pass


def setup_logging(level: str) -> None:
    """Send logs to stdout.

    Python logs to stderr by default, and PowerShell renders anything a native
    command writes there as a NativeCommandError record — so a perfectly
    healthy run looks like a wall of red. These lines are this tool's primary
    output, not errors, so stdout is where they belong; genuine failures are
    still printed to stderr by the commands themselves.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _components(config: Config):
    store = Store(config.db_path)
    budget = RequestBudget(store, config.max_requests_per_hour, config.max_requests_per_day)
    api = build_client(config, budget=budget, market_cache=store)
    telegram = TelegramClient(
        config.telegram_bot_token,
        config.telegram_chat_id,
        timeout=config.request_timeout_seconds,
        dry_run=config.dry_run,
    )
    return store, budget, api, telegram


def cmd_check(config: Config) -> int:
    store, budget, api, telegram = _components(config)
    ok = True
    try:
        me = telegram.get_me() or {}
        bot = (me.get("result") or {}).get("username", "?")
        print(f"✓ Telegram bot reachable: @{bot}")
        telegram.send_message(
            "✅ <b>odds-watcher</b> is configured.\n"
            f"Books: {', '.join(config.bookmakers)}\n"
            f"Alerting on drops ≥ {config.min_drop_pct:.1f}% {config.alert_window_label}."
        )
        print(f"✓ Test message sent to chat {config.telegram_chat_id}")
    except (TransportError, BudgetExceeded) as exc:
        ok = False
        print(f"✗ Telegram: {exc}", file=sys.stderr)
        _budget_hint(config, exc)

    try:
        selected = api.get_selected_bookmakers()
        print(f"✓ odds-api.io reachable. Selected bookmakers: {selected or '(none)'}")
        missing = [b for b in config.bookmakers if b.lower() not in selected]
        if missing:
            print(
                f"! {', '.join(missing)} not selected on the account — "
                "run `python -m odds_watcher select-bookmakers`"
            )
    except (TransportError, BudgetExceeded) as exc:
        ok = False
        print(f"✗ odds-api.io: {exc}", file=sys.stderr)
        _budget_hint(config, exc)

    try:
        now = now_ts()
        events = api.get_events(config.sports[0])
        upcoming = sorted(
            (e for e in events if e.seconds_to_start(now) > 0), key=lambda e: e.start_ts
        )
        print(f"✓ {len(events)} {config.sports[0]} event(s) returned, {len(upcoming)} still upcoming")
        for event in upcoming[:5]:
            print(
                f"   · {event.name} — {format_clock(event.start_ts)} "
                f"(starts in {format_countdown(event.seconds_to_start(now))})"
            )
        if not upcoming:
            print("! nothing upcoming — check the SPORTS/LEAGUES slugs", file=sys.stderr)
        _report_budget_fit(config, upcoming, now)
    except (TransportError, BudgetExceeded) as exc:
        ok = False
        print(f"✗ events endpoint: {exc}", file=sys.stderr)
        _budget_hint(config, exc)
        _sport_error(api, config, exc)

    hour, day = budget.remaining()
    unit = "credit" if config.odds_provider == "the-odds-api" else "request"
    print(f"· local {unit} budget left: {hour}/hour, {day}/day")
    remaining = getattr(api, "credits_remaining", None)
    if remaining is not None:
        print(f"· The Odds API account credits remaining: {remaining}")
    store.close()
    return 0 if ok else 1


def cmd_bookmakers(config: Config, search: Optional[str] = None) -> int:
    """List the identifiers /bookmakers/selected/select will accept."""
    store, _, api, _ = _components(config)
    try:
        rows = api.get_bookmakers()
    except (TransportError, BudgetExceeded) as exc:
        print(f"✗ could not list bookmakers: {exc}", file=sys.stderr)
        _budget_hint(config, exc)
        return 1
    finally:
        store.close()

    if search:
        needle = search.lower()
        rows = [row for row in rows if needle in row[0].lower() or needle in row[1].lower()]
    if not rows:
        print(f"no bookmakers matching {search!r}", file=sys.stderr)
        return 1
    width = max(len(identifier) for identifier, _ in rows)
    for identifier, label in rows:
        print(f"{identifier.ljust(width)}  {label}")
    print(f"\n{len(rows)} bookmaker(s). Put the left-hand identifiers in BOOKMAKERS in your .env.")
    return 0


def _print_rows(rows: list, noun: str, setting: str, search: Optional[str]) -> int:
    if search:
        needle = search.lower()
        rows = [row for row in rows if needle in row[0].lower() or needle in row[1].lower()]
    if not rows:
        print(f"no {noun} matching {search!r}", file=sys.stderr)
        return 1
    width = max(len(identifier) for identifier, _ in rows)
    for identifier, label in rows:
        print(f"{identifier.ljust(width)}  {label}")
    print(f"\n{len(rows)} {noun}. Put the left-hand identifiers in {setting} in your .env.")
    return 0


def cmd_sports(config: Config, search: Optional[str] = None, include_all: bool = False) -> int:
    """List the sport identifiers the API recognises, for SPORTS."""
    store, _, api, _ = _components(config)
    try:
        rows = api.get_sports(include_all=include_all)
    except (TransportError, BudgetExceeded) as exc:
        print(f"✗ could not list sports: {exc}", file=sys.stderr)
        _budget_hint(config, exc)
        return 1
    finally:
        store.close()
    result = _print_rows(rows, "sport(s)", "SPORTS", search)
    if result == 0 and config.odds_provider == "the-odds-api" and not include_all:
        print("In-season only — add --all to include out-of-season competitions.")
    return result


def cmd_leagues(config: Config, search: Optional[str] = None) -> int:
    """List league identifiers for the configured sports, for LEAGUES."""
    store, _, api, _ = _components(config)
    rows: list[tuple[str, str]] = []
    try:
        for sport in config.sports:
            rows.extend(api.get_leagues(sport))
    except UnsupportedByProvider as exc:
        print(f"! {exc}\n")
        return cmd_sports(config, search, include_all=True)
    except (TransportError, BudgetExceeded) as exc:
        print(f"✗ could not list leagues: {exc}", file=sys.stderr)
        _budget_hint(config, exc)
        _sport_error(api, config, exc)
        return 1
    finally:
        store.close()

    rows = sorted(set(rows))
    if search:
        needle = search.lower()
        rows = [row for row in rows if needle in row[0].lower() or needle in row[1].lower()]
    if not rows:
        print(f"no leagues matching {search!r}", file=sys.stderr)
        return 1
    width = max(len(identifier) for identifier, _ in rows)
    for identifier, label in rows:
        print(f"{identifier.ljust(width)}  {label}")
    print(f"\n{len(rows)} league(s). Put the left-hand identifiers in LEAGUES in your .env.")
    return 0


def _suggest_sports(api, wanted: tuple) -> None:
    """After a rejected sport, show the slugs that resemble what was asked for.

    Sport slugs are lowercase; "Baseball" is rejected where "baseball" works,
    which is not something an error message should leave you to guess.
    """
    try:
        rows = api.get_sports()
    except TransportError:
        return
    known = {identifier for identifier, _ in rows}
    for name in wanted:
        if name in known:
            continue
        needle = name.strip().lower()
        matches = [i for i, label in rows if needle in i.lower() or needle in label.lower()]
        if matches:
            print(f"  did you mean, for {name!r}: {', '.join(matches[:8])}")
        else:
            print(f"  no sport resembling {name!r}; run `py -m odds_watcher sports` for the full list")


def _budget_hint(config: Config, exc) -> None:
    """Explain an exhausted local allowance and how to move past it."""
    if not isinstance(exc, BudgetExceeded):
        return
    unit = "credits" if config.odds_provider == "the-odds-api" else "requests"
    print(
        f"\nThis is the watcher's own cap, not the provider's: it allows "
        f"{config.max_requests_per_hour} {unit}/hour and "
        f"{config.max_requests_per_day}/day.",
        file=sys.stderr,
    )
    print(
        "  Raise MAX_REQUESTS_PER_HOUR / MAX_REQUESTS_PER_DAY in .env, wait for the\n"
        "  rolling 24h window to free up, or clear the local tally with:\n"
        "    py -m odds_watcher status --reset-budget",
        file=sys.stderr,
    )


def _sport_error(api, config: Config, exc) -> None:
    """Explain a failure that is really a bad sport slug."""
    if "sport" not in str(exc).lower():
        return
    print("\nThe sport identifier in SPORTS is not one this API accepts (slugs are lowercase).")
    _suggest_sports(api, config.sports)


def _suggest_bookmakers(api, wanted: tuple) -> None:
    """After a rejected selection, show identifiers that look like what was asked for."""
    try:
        rows = api.get_bookmakers()
    except TransportError:
        return
    for name in wanted:
        needle = name.lower()
        matches = [i for i, label in rows if needle in i.lower() or needle in label.lower()]
        if matches:
            print(f"  did you mean, for {name!r}: {', '.join(matches[:8])}")
        else:
            print(f"  nothing resembling {name!r} in the bookmaker list")


def _report_budget_fit(config: Config, upcoming: list, now: float) -> None:
    """Warn if the current slate would out-poll the free tier's allowance.

    Every poll spends one request per 20 fixtures inside the tracking lead, so
    a wide slate silently exhausts the hourly budget. Better to say so here
    than to have the watcher start skipping polls overnight.
    """
    import math

    from .watcher import EVENTS_PER_ODDS_REQUEST

    in_range = [
        event
        for event in upcoming
        if config.window_end_seconds <= event.seconds_to_start(now) <= config.baseline_lead_seconds
    ]
    polls_per_hour = 3600 / config.poll_interval_seconds
    if config.per_event_odds:
        per_poll = len(in_range)
    else:
        per_poll = math.ceil(len(in_range) / EVENTS_PER_ODDS_REQUEST) if in_range else 0
    hourly = int(per_poll * polls_per_hour)

    print(f"· {len(in_range)} fixture(s) inside the {config.baseline_lead_seconds // 60}-min tracking lead right now")
    if hourly > config.max_requests_per_hour:
        print(
            f"! at this rate that is ~{hourly} requests/hour, over your "
            f"{config.max_requests_per_hour}/hour cap.\n"
            f"  Narrow LEAGUES, or raise POLL_INTERVAL_SECONDS to "
            f"{math.ceil(3600 * per_poll / config.max_requests_per_hour)}+."
        )
    elif hourly:
        print(f"· ~{hourly} requests/hour at the current poll interval (cap {config.max_requests_per_hour})")


def cmd_select_bookmakers(config: Config) -> int:
    store, _, api, _ = _components(config)
    try:
        api.select_bookmakers(config.bookmakers)
        print(f"✓ account bound to: {', '.join(api.get_selected_bookmakers())}")
        return 0
    except UnsupportedByProvider as exc:
        print(f"! {exc}")
        return 0
    except (TransportError, BudgetExceeded) as exc:
        print(f"✗ could not select bookmakers: {exc}", file=sys.stderr)
        _budget_hint(config, exc)
        print("\nThe identifiers in BOOKMAKERS are not the ones this API uses.")
        _suggest_bookmakers(api, config.bookmakers)
        print("\nFull list: py -m odds_watcher bookmakers --search bet")
        return 1
    finally:
        store.close()


def cmd_chat_id(config: Config) -> int:
    _, _, _, telegram = _components(config)
    try:
        updates = telegram.get_updates()
    except (TransportError, BudgetExceeded) as exc:
        print(f"✗ could not reach Telegram: {exc}", file=sys.stderr)
        _budget_hint(config, exc)
        return 1
    if not updates:
        print(
            "No updates yet. Send any message to your bot in Telegram, then run this again.",
            file=sys.stderr,
        )
        return 1
    seen = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            seen[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name", "")
    for chat_id, name in seen.items():
        print(f"{chat_id}\t{name}")
    return 0


def _installed_revision() -> str:
    """The git revision this code is running from, for spotting a stale copy.

    Every setting and command is version-specific, so "that flag does not
    exist" is usually an un-pulled checkout rather than a bug.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent), "log", "-1",
             "--format=%h %cs %s"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown (git unavailable)"
    if result.returncode != 0:
        return "unknown (not a git checkout)"
    return result.stdout.strip() or "unknown"


def _env_keys(path: Path) -> list:
    """Setting names defined in an env-style file, in order."""
    if not path.is_file():
        return []
    keys = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.partition("=")[0].strip())
    return keys


def _report_missing_settings(env_file: Path) -> None:
    """Name settings that exist in .env.example but not in the user's .env.

    .env is gitignored, so a pull never adds newly introduced settings to it.
    Silently falling back to defaults is how a setting you meant to change
    ends up having no effect at all.
    """
    example = env_file.parent / ".env.example"
    if not example.is_file() or not env_file.is_file():
        return
    missing = [key for key in _env_keys(example) if key not in set(_env_keys(env_file))]
    if not missing:
        return
    print(f"\n! {len(missing)} setting(s) in .env.example are absent from {env_file.name};")
    print("  their defaults apply. Copy across any you want to change:")
    for key in missing:
        print(f"    {key}")


def cmd_usage(config: Config, check_balance: bool = False) -> int:
    """Local spend, the provider's own balance, and the resulting burn rate."""
    store, budget, api, _ = _components(config)
    now = now_ts()
    unit = "credits" if config.odds_provider == "the-odds-api" else "requests"

    last_hour = store.count_calls_since(now - 3600)
    last_day = store.count_calls_since(now - 86400)
    hour_left, day_left = budget.remaining()

    print(f"provider:        {config.odds_provider}")
    print(f"spent last hour: {last_hour} {unit}  (local cap {config.max_requests_per_hour}, "
          f"{hour_left} left)")
    print(f"spent last 24h:  {last_day} {unit}  (local cap {config.max_requests_per_day}, "
          f"{day_left} left)")

    quota = None
    free_balance_check = config.odds_provider == "the-odds-api"
    if hasattr(api, "fetch_quota") and (free_balance_check or check_balance):
        try:
            quota = api.fetch_quota()
        except (TransportError, BudgetExceeded) as exc:
            print(f"\n! could not read the account balance: {exc}", file=sys.stderr)
    store.close()

    if quota is None and hasattr(api, "fetch_quota") and not free_balance_check:
        print("\n· account balance not checked (costs one request): "
              "py -m odds_watcher usage --check-balance")

    if quota is not None and quota.get("remaining") is None:
        print(
            "\n· the provider did not report a remaining allowance in its response.\n"
            "  Check it on your account dashboard; the local figures above are this\n"
            "  watcher's own count of requests it made, not the provider's."
        )
    if quota and quota.get("remaining") is not None:
        remaining = quota["remaining"]
        print(f"\naccount credits: {remaining} remaining, {quota.get('used')} used")
        if last_hour:
            hours = remaining / last_hour
            print(f"burn rate:       {last_hour}/hour -> about {hours:.1f} hour(s) of headroom")
            if hours < 24:
                print("!  at this rate the account runs dry within a day. Raise")
                print("   POLL_INTERVAL_SECONDS, narrow SPORTS, or drop PROP_MARKETS.")
        else:
            print("burn rate:       nothing spent in the last hour")
    return 0


def cmd_status(config: Config, env_file: Optional[Path] = None, reset: bool = False) -> int:
    store, budget, _, _ = _components(config)
    if reset:
        store.conn.execute("DELETE FROM api_calls")
        store.conn.commit()
        print("local budget tally cleared (the provider's own usage is unaffected)\n")
    hour, day = budget.remaining()
    unit = "credits" if config.odds_provider == "the-odds-api" else "requests"
    print(f"version:        {__version__} @ {_installed_revision()}")
    print(f"provider:       {config.odds_provider}")
    print(f"database:       {config.db_path}")
    print(f"tracked lines:  {store.tracked_lines()}")
    print(f"budget left:    {hour}/hour, {day}/day ({unit})")
    print(f"alert rule:     drop ≥ {config.min_drop_pct:.1f}% {config.alert_window_label}")
    print(f"bookmakers:     {', '.join(config.bookmakers)}")
    store.close()
    if env_file is not None:
        _report_missing_settings(env_file)
    return 0


MARKET_SAMPLE_SIZE = 10


def cmd_discover_markets(config: Config) -> int:
    """Find which non-featured market keys your account actually returns.

    The Odds API only sends markets that were named in the request and has no
    endpoint listing them, so the documented keys are requested against a live
    fixture and the ones that answer are reported.
    """
    from .market_keys import candidates, known_sports

    store, _, api, _ = _components(config)
    sport = config.sports[0] if config.sports else ""
    keys = candidates(sport)
    if not keys:
        print(
            f"No candidate market keys are catalogued for {sport!r}.\n"
            f"Catalogued sports: {', '.join(known_sports())}",
            file=sys.stderr,
        )
        store.close()
        return 1

    cost = len(keys) * max(len(config.regions), 1)
    print(f"probing {len(keys)} market key(s) for {sport} — costs up to {cost} credit(s)\n")

    try:
        now = now_ts()
        events = _upcoming(api, config, now, 1)
        if not events:
            print("no upcoming fixture to probe", file=sys.stderr)
            return 1
        event = events[0]
        print(f"fixture: {event.name}\n")
        result = api.discover_markets(event.id, keys, config.bookmakers)
    except (TransportError, BudgetExceeded) as exc:
        print(f"✗ market discovery failed: {exc}", file=sys.stderr)
        _budget_hint(config, exc)
        _sport_error(api, config, exc)
        return 1
    finally:
        store.close()

    available = result["available"]
    if available:
        width = max(len(key) for key in available)
        print(f"returning prices ({len(available)}):")
        for key, books in sorted(available.items()):
            offered = ", ".join(f"{b} ({c})" for b, c in sorted(books.items()))
            print(f"    {key.ljust(width)}  {offered}")
    else:
        print("no candidate key returned prices for this fixture.")

    if result["empty"]:
        print(f"\naccepted but empty for this fixture ({len(result['empty'])}):")
        print("    " + ", ".join(sorted(result["empty"])[:20]))
    if result["rejected"]:
        print(f"\nrejected by the API ({len(result['rejected'])}):")
        print("    " + ", ".join(sorted(result["rejected"])))

    if available:
        props = sorted(available)
        print("\nAdd to .env — note each key costs a credit per fixture per poll:\n")
        print("PROP_MARKETS=" + ",".join(props))
    remaining = getattr(api, "credits_remaining", None)
    if remaining is not None:
        print(f"\naccount credits remaining: {remaining}")
    return 0


def cmd_markets(config: Config, search: Optional[str] = None) -> int:
    """List every market name the configured books offer on upcoming fixtures.

    Market availability varies by fixture — corners and bookings markets often
    only exist on bigger games — so this samples several fixtures at once, in a
    single batched request, and reports which books priced each market.
    """
    store, _, api, _ = _components(config)
    _parse, _catalogue_fn = _parsers(config)
    catalogue: dict = {}
    try:
        now = now_ts()
        upcoming = _upcoming(api, config, now, MARKET_SAMPLE_SIZE, spread=True)
        if not upcoming:
            print("no upcoming fixtures to sample", file=sys.stderr)
            return 1
        blocks = api.get_odds_payloads([e.id for e in upcoming], config.bookmakers)
        for name, books in _catalogue_fn(blocks).items():
            entry = catalogue.setdefault(name, {})
            for book, count in books.items():
                entry[book] = entry.get(book, 0) + count
    except (TransportError, BudgetExceeded) as exc:
        print(f"✗ could not list markets: {exc}", file=sys.stderr)
        _budget_hint(config, exc)
        _sport_error(api, config, exc)
        return 1
    finally:
        store.close()

    rows = sorted(catalogue.items())
    if search:
        needle = search.lower()
        rows = [row for row in rows if needle in row[0].lower()]
    if not rows:
        print(f"no markets matching {search!r}", file=sys.stderr)
        return 1

    width = max(len(name) for name, _ in rows)
    print(f"markets seen across {len(upcoming)} fixture(s) sampled across the upcoming slate:\n")
    for name, books in rows:
        offered = ", ".join(f"{book} ({count})" for book, count in sorted(books.items()))
        print(f"  {name.ljust(width)}  {offered}")
    print(f"\n{len(rows)} market(s). Put the names in MARKETS in your .env.")
    print("Matching is case-insensitive substring; prefix with - to exclude, e.g. Totals,-HT")
    return 0


PROBE_SAMPLE_SIZE = 10


def _upcoming(api, config: Config, now: float, limit: int, *, spread: bool = False) -> list:
    """Upcoming fixtures: the next `limit`, or a sample spread across them all.

    Which leagues are in the *next* few fixtures depends entirely on the hour
    of day — at 21:00 UTC a worldwide football feed is all South America. For
    discovery (markets, coverage) that bias hides everything your books price
    on other continents, so those commands sample evenly across the whole
    upcoming horizon instead.
    """
    events = api.get_events(config.sports[0])
    upcoming = sorted((e for e in events if e.seconds_to_start(now) > 0), key=lambda e: e.start_ts)
    if not spread or len(upcoming) <= limit:
        return upcoming[:limit]
    stride = len(upcoming) / limit
    return [upcoming[int(index * stride)] for index in range(limit)]


def _parsers(config: Config):
    """The active provider's ``(parse_quotes, market_catalogue)`` pair.

    Selected from config rather than from the client, so the payload parsers
    stay plain functions that can be exercised without a client at all.
    """
    if config.odds_provider == "the-odds-api":
        from . import theoddsapi as provider
    elif config.odds_provider == "parlay-api":
        from . import parlayapi as provider
    else:
        from . import odds_api as provider
    return provider.parse_quotes, provider.market_catalogue


def _books_in_block(block: dict, config: Config) -> dict:
    """``{bookmaker: price count}`` for one raw odds block."""
    _parse, catalogue = _parsers(config)
    counts: dict = {}
    for books in catalogue([block]).values():
        for book, count in books.items():
            counts[book] = counts.get(book, 0) + count
    return counts


def cmd_props(config: Config) -> int:
    """Determine whether player props require per-fixture requests.

    odds-api.io documents props as being available one event at a time, which
    suggests the batched /odds/multi response may carry only game markets.
    That distinction decides the request budget entirely, so this fetches the
    same fixture both ways and compares what each returns.
    """
    from .watcher import EVENTS_PER_ODDS_REQUEST

    store, _, api, _ = _components(config)
    _parse, _catalogue_fn = _parsers(config)
    try:
        now = now_ts()
        events = _upcoming(api, config, now, PROBE_SAMPLE_SIZE)
        if not events:
            print("no upcoming fixtures to inspect", file=sys.stderr)
            return 1

        batched = api.get_odds_payloads([e.id for e in events], config.bookmakers)
        target, batched_markets = None, {}
        for block in batched:
            if not isinstance(block, dict):
                continue
            catalogue = _catalogue_fn([block])
            if catalogue:
                event_id = str(block.get("id") or block.get("eventId") or "")
                target = next((e for e in events if e.id == event_id), None)
                batched_markets = catalogue
                break
        if target is None:
            target = events[0]
            print(f"no fixture in the sample had batched prices; inspecting {target.name}\n")

        single = _catalogue_fn([api.get_event_odds_raw(target.id, config.bookmakers)])
    except (TransportError, BudgetExceeded) as exc:
        print(f"✗ props check failed: {exc}", file=sys.stderr)
        _budget_hint(config, exc)
        _sport_error(api, config, exc)
        return 1
    finally:
        store.close()

    print(f"fixture: {target.name}  ({target.league or 'unknown league'})\n")
    print(f"  /odds/multi (batched)   : {len(batched_markets):>4} market(s)")
    print(f"  /odds       (per fixture): {len(single):>4} market(s)")

    only_single = sorted(set(single) - set(batched_markets))
    only_batched = sorted(set(batched_markets) - set(single))

    def _listing(names: list, where: str) -> None:
        print(f"\nonly in the {where} response ({len(names)}):")
        for name in names[:25]:
            print(f"    {name}")
        if len(names) > 25:
            print(f"    ... and {len(names) - 25} more")

    if only_single:
        _listing(only_single, "per-fixture")
    if only_batched:
        _listing(only_batched, "batched")

    print()
    if only_single and not only_batched:
        print("=> the per-fixture endpoint returns more. Set PER_EVENT_ODDS=true to")
        print("   collect these, at the cost of one request per fixture per poll.")
        _per_event_budget(config, events, now)
    elif only_batched and not only_single:
        print("=> the batched endpoint returns MORE than the per-fixture one, so there")
        print("   is nothing to gain by requesting fixtures individually.")
        print(f"   Keep PER_EVENT_ODDS=false ({EVENTS_PER_ODDS_REQUEST} fixtures per request).")
    elif only_single and only_batched:
        print("=> each endpoint returns markets the other does not. Batched is the")
        print("   cheaper source; enable PER_EVENT_ODDS only if the per-fixture-only")
        print("   markets above are ones you actually want.")
        _per_event_budget(config, events, now)
    else:
        print("=> both endpoints return the same markets, so batching loses nothing.")
        print(f"   Keep PER_EVENT_ODDS=false ({EVENTS_PER_ODDS_REQUEST} fixtures per request).")

    everything = set(batched_markets) | set(single)
    props = sorted(name for name in everything if _looks_like_a_prop(name))
    print()
    if props:
        print(f"player-prop style markets found ({len(props)}):")
        for name in props[:15]:
            source = "batched" if name in batched_markets else "per-fixture"
            print(f"    {name}  [{source}]")
        if len(props) > 15:
            print(f"    ... and {len(props) - 15} more")
    else:
        print("!  Nothing that looks like a player-prop market in either response.")
        print("   Props may not be on your plan, or not offered for this fixture.")
    return 0


# Prop markets are named by statistic, not by the word "prop" — Pitcher
# Strikeouts O/U and Home Runs O/U are props; Run Line and Totals are not.
_PROP_WORDS = (
    "player", "prop", "pitcher", "batter", "strikeout", "home run", "total bases",
    "hits", "rbi", "walks", "singles", "doubles", "triples", "stolen base",
    "earned run", "outs recorded", "to record", "to hit", "anytime",
)


def _looks_like_a_prop(market: str) -> bool:
    name = market.lower()
    return any(word in name for word in _PROP_WORDS)


def _per_event_budget(config: Config, events: list, now: float) -> None:
    """Spell out what per-fixture polling would cost against the hourly cap."""
    import math

    in_range = [
        e for e in events
        if config.window_end_seconds <= e.seconds_to_start(now) <= config.baseline_lead_seconds
    ]
    count = len(in_range) or len(events)
    hourly = int(count * 3600 / config.poll_interval_seconds)
    print(f"\n   with ~{count} fixture(s) in range that is ~{hourly} requests/hour")
    if hourly > config.max_requests_per_hour:
        needed = math.ceil(count * 3600 / config.max_requests_per_hour)
        print(f"   — over your {config.max_requests_per_hour}/hour cap. It would fit only at")
        print(f"     POLL_INTERVAL_SECONDS={needed}, which may be too slow for a "
              f"{config.window_start_seconds // 60}-minute window.")
        print("     Narrowing LEAGUES to fewer simultaneous fixtures is the way out.")


def cmd_probe(config: Config) -> int:
    """Sample upcoming fixtures and report which ones your books actually price.

    Coverage is the thing that decides whether this bot ever fires: a fixture
    no watched bookmaker quotes yields an empty payload, which is not a fault
    to debug. Sampling several fixtures in one batched request distinguishes
    "wrong configuration" from "these leagues simply are not priced".
    """
    import json

    store, _, api, _ = _components(config)
    try:
        now = now_ts()
        events = _upcoming(api, config, now, PROBE_SAMPLE_SIZE)
        if not events:
            print("no upcoming fixtures to probe", file=sys.stderr)
            return 1
        by_id = {event.id: event for event in events}
        blocks = api.get_odds_payloads([e.id for e in events], config.bookmakers)
    except (TransportError, BudgetExceeded) as exc:
        print(f"✗ probe failed: {exc}", file=sys.stderr)
        _budget_hint(config, exc)
        _sport_error(api, config, exc)
        return 1
    finally:
        store.close()

    print(f"sampled {len(events)} upcoming fixture(s), asking for: {', '.join(config.bookmakers)}\n")

    priced, empty = [], []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        event = by_id.get(str(block.get("id") or block.get("eventId") or ""))
        counts = _books_in_block(block, config)
        (priced if counts else empty).append((event, block, counts))

    for event, _block, counts in priced:
        name = event.name if event else "?"
        league = f" ({event.league})" if event and event.league else ""
        books = ", ".join(f"{b} ({c})" for b, c in sorted(counts.items()))
        print(f"  ✓ {name}{league}\n      {books}")
    for event, _block, _counts in empty:
        name = event.name if event else "?"
        league = f" ({event.league})" if event and event.league else ""
        print(f"  ✗ {name}{league} — no prices from any watched book")

    wanted = {b.lower() for b in config.bookmakers}
    seen = {book for _e, _b, counts in priced for book in counts}
    print(f"\n{len(priced)}/{len(blocks)} sampled fixture(s) priced by at least one watched book")

    if not priced:
        print("\n✗ nothing priced. This is a coverage problem, not a parsing one:")
        print("  these leagues are not quoted by your books. Narrow LEAGUES to")
        print("  competitions they cover, or watch a book with wider coverage.")
        if blocks:
            print("\nOne raw payload for reference:\n")
            print(json.dumps(blocks[0], indent=2)[:1500])
        return 1

    for book in sorted(wanted - seen):
        print(f"! {book} priced none of the sampled fixtures")

    example = priced[0]
    parse_quotes, _ = _parsers(config)
    quotes = parse_quotes([example[1]], default_event_id=example[0].id if example[0] else None)
    if quotes:
        print(f"\nsample prices from {example[0].name if example[0] else '?'}:")
        for quote in quotes[:8]:
            print(f"    {quote.bookmaker:12} {quote.label}: {quote.odds:.2f}")
    return 0


COVERAGE_SAMPLE_SIZE = 60


def cmd_coverage(config: Config) -> int:
    """Report, per league, how many upcoming fixtures the watched books price.

    A worldwide sport returns thousands of fixtures, most of them in leagues no
    major bookmaker quotes. This samples the next fixtures in batched requests
    and groups the result by league, so LEAGUES can be set to competitions that
    actually produce prices instead of guessed at.
    """
    from .watcher import EVENTS_PER_ODDS_REQUEST, chunked

    store, _, api, _ = _components(config)
    try:
        now = now_ts()
        events = _upcoming(api, config, now, COVERAGE_SAMPLE_SIZE, spread=True)
        if not events:
            print("no upcoming fixtures to sample", file=sys.stderr)
            return 1
        by_id = {event.id: event for event in events}
        blocks = []
        for batch in chunked([e.id for e in events], EVENTS_PER_ODDS_REQUEST):
            blocks.extend(api.get_odds_payloads(batch, config.bookmakers))
    except (TransportError, BudgetExceeded) as exc:
        print(f"✗ coverage check failed: {exc}", file=sys.stderr)
        _budget_hint(config, exc)
        _sport_error(api, config, exc)
        return 1
    finally:
        store.close()

    leagues: dict = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        event = by_id.get(str(block.get("id") or block.get("eventId") or ""))
        if event is None:
            continue
        entry = leagues.setdefault(
            event.league or "unknown", {"slug": event.league_slug, "sampled": 0, "books": {}}
        )
        entry["sampled"] += 1
        for book in _books_in_block(block, config):
            entry["books"][book] = entry["books"].get(book, 0) + 1

    if not leagues:
        print("no fixtures came back with odds at all", file=sys.stderr)
        return 1

    wanted = [b.lower() for b in config.bookmakers]
    ranked = sorted(
        leagues.items(),
        key=lambda kv: (-sum(kv[1]["books"].values()), kv[0]),
    )
    width = min(max(len(name) for name in leagues), 46)
    span = format_countdown(max(e.seconds_to_start(now) for e in events))
    print(
        f"coverage across {len(blocks)} fixture(s) sampled over the next {span}, "
        f"books: {', '.join(config.bookmakers)}\n"
    )
    print(f"  {'league'.ljust(width)}  sampled  " + "  ".join(b.ljust(11) for b in wanted))
    for name, entry in ranked:
        cells = "  ".join(str(entry["books"].get(book, 0)).ljust(11) for book in wanted)
        print(f"  {name[:width].ljust(width)}  {str(entry['sampled']).ljust(7)}  {cells}")

    covered = [
        entry["slug"] or name
        for name, entry in ranked
        if all(entry["books"].get(book) for book in wanted)
    ]
    print()
    if covered:
        print("leagues priced by every watched book — a good starting LEAGUES:\n")
        print("LEAGUES=" + ",".join(covered[:12]))
    else:
        partial = [entry["slug"] or name for name, entry in ranked if entry["books"]]
        if partial:
            print("no league in this sample was priced by *all* watched books.")
            print("Priced by at least one:\n")
            print("LEAGUES=" + ",".join(partial[:12]))
        else:
            print("nothing in this sample was priced. Your books do not quote these")
            print("leagues at all — sample again when a bigger slate is upcoming.")
    return 0


def _report(alerts: list[Alert]) -> None:
    if not alerts:
        print("no drops detected in this poll")
        return
    for alert in alerts:
        print(
            f"{alert.event.name} · {alert.quote.bookmaker} · {alert.quote.label}: "
            f"{alert.reference_odds:.2f} -> {alert.quote.odds:.2f} (-{alert.drop_pct:.1f}%)"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    force_utf8_output()
    load_dotenv(Path(args.env_file))
    try:
        config = Config.from_env(required=REQUIRED_CREDENTIALS.get(args.command, ALL_CREDENTIALS))
    except ConfigError as exc:
        setup_logging(args.log_level or "INFO")
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        config = replace_dry_run(config)
    if args.sport:
        # Discovery must not depend on .env already being right — that is the
        # thing you are using these commands to work out.
        import dataclasses

        config = dataclasses.replace(config, sports=(args.sport,))
    setup_logging(args.log_level or config.log_level)

    if args.command == "check":
        return cmd_check(config)
    if args.command == "select-bookmakers":
        return cmd_select_bookmakers(config)
    if args.command == "bookmakers":
        return cmd_bookmakers(config, args.search)
    if args.command == "sports":
        return cmd_sports(config, args.search, args.include_all)
    if args.command == "leagues":
        return cmd_leagues(config, args.search)
    if args.command == "probe":
        return cmd_probe(config)
    if args.command == "markets":
        if args.discover:
            return cmd_discover_markets(config)
        return cmd_markets(config, args.search)
    if args.command == "coverage":
        return cmd_coverage(config)
    if args.command == "props":
        return cmd_props(config)
    if args.command == "chat-id":
        return cmd_chat_id(config)
    if args.command == "usage":
        return cmd_usage(config, args.check_balance)
    if args.command == "status":
        return cmd_status(config, Path(args.env_file), args.reset_budget)

    store, _, api, telegram = _components(config)
    watcher = Watcher(config, api, telegram, store)
    try:
        if args.command == "once":
            _report(watcher.poll_once())
            return 0
        watcher.run_forever()
        return 0
    except KeyboardInterrupt:
        log.info("stopped")
        return 0
    finally:
        store.close()


def replace_dry_run(config: Config) -> Config:
    import dataclasses

    return dataclasses.replace(config, dry_run=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
