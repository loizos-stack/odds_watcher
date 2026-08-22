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

from .config import ALL_CREDENTIALS, Config, ConfigError, load_dotenv
from .detector import Alert
from .http import TransportError
from .odds_api import OddsApiClient
from .store import RequestBudget, Store
from .telegram import TelegramClient
from .util import format_clock, format_countdown, now_ts
from .watcher import Watcher

log = logging.getLogger("odds_watcher")


# Each command only demands the credentials it actually uses.
REQUIRED_CREDENTIALS = {
    "chat-id": ("TELEGRAM_BOT_TOKEN",),
    "bookmakers": ("ODDS_API_KEY",),
    "leagues": ("ODDS_API_KEY",),
    "probe": ("ODDS_API_KEY",),
    "markets": ("ODDS_API_KEY",),
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
        choices=["run", "once", "check", "select-bookmakers", "bookmakers", "leagues", "markets", "probe", "chat-id", "status"],
    )
    parser.add_argument(
        "--search",
        default=None,
        help="filter the `bookmakers` / `leagues` listing, e.g. --search premier",
    )
    parser.add_argument("--env-file", default=".env", help="path to the .env file (default: .env)")
    parser.add_argument("--log-level", default=None, help="override LOG_LEVEL")
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
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _components(config: Config):
    store = Store(config.db_path)
    budget = RequestBudget(store, config.max_requests_per_hour, config.max_requests_per_day)
    api = OddsApiClient(
        config.odds_api_key,
        base_url=config.api_base_url,
        timeout=config.request_timeout_seconds,
        budget=budget,
    )
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
    except TransportError as exc:
        ok = False
        print(f"✗ Telegram: {exc}", file=sys.stderr)

    try:
        selected = api.get_selected_bookmakers()
        print(f"✓ odds-api.io reachable. Selected bookmakers: {selected or '(none)'}")
        missing = [b for b in config.bookmakers if b.lower() not in selected]
        if missing:
            print(
                f"! {', '.join(missing)} not selected on the account — "
                "run `python -m odds_watcher select-bookmakers`"
            )
    except TransportError as exc:
        ok = False
        print(f"✗ odds-api.io: {exc}", file=sys.stderr)

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
    except TransportError as exc:
        ok = False
        print(f"✗ events endpoint: {exc}", file=sys.stderr)

    hour, day = budget.remaining()
    print(f"· request budget left: {hour}/hour, {day}/day")
    store.close()
    return 0 if ok else 1


def cmd_bookmakers(config: Config, search: Optional[str] = None) -> int:
    """List the identifiers /bookmakers/selected/select will accept."""
    store, _, api, _ = _components(config)
    try:
        rows = api.get_bookmakers()
    except TransportError as exc:
        print(f"✗ could not list bookmakers: {exc}", file=sys.stderr)
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


def cmd_leagues(config: Config, search: Optional[str] = None) -> int:
    """List league identifiers for the configured sports, for LEAGUES."""
    store, _, api, _ = _components(config)
    rows: list[tuple[str, str]] = []
    try:
        for sport in config.sports:
            rows.extend(api.get_leagues(sport))
    except TransportError as exc:
        print(f"✗ could not list leagues: {exc}", file=sys.stderr)
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
    except TransportError as exc:
        print(f"✗ could not select bookmakers: {exc}", file=sys.stderr)
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
    except TransportError as exc:
        print(f"✗ could not reach Telegram: {exc}", file=sys.stderr)
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


def cmd_status(config: Config) -> int:
    store, budget, _, _ = _components(config)
    hour, day = budget.remaining()
    print(f"database:       {config.db_path}")
    print(f"tracked lines:  {store.tracked_lines()}")
    print(f"budget left:    {hour}/hour, {day}/day")
    print(f"alert rule:     drop ≥ {config.min_drop_pct:.1f}% {config.alert_window_label}")
    print(f"bookmakers:     {', '.join(config.bookmakers)}")
    store.close()
    return 0


MARKET_SAMPLE_SIZE = 10


def cmd_markets(config: Config, search: Optional[str] = None) -> int:
    """List every market name the configured books offer on upcoming fixtures.

    Market availability varies by fixture — corners and bookings markets often
    only exist on bigger games — so this samples several fixtures at once, in a
    single batched request, and reports which books priced each market.
    """
    from .odds_api import market_catalogue

    store, _, api, _ = _components(config)
    catalogue: dict = {}
    try:
        now = now_ts()
        events = api.get_events(config.sports[0])
        upcoming = sorted(
            (e for e in events if e.seconds_to_start(now) > 0), key=lambda e: e.start_ts
        )[:MARKET_SAMPLE_SIZE]
        if not upcoming:
            print("no upcoming fixtures to sample", file=sys.stderr)
            return 1
        payload = api.get_multi_odds_raw([e.id for e in upcoming], config.bookmakers)
        for name, books in market_catalogue(payload).items():
            entry = catalogue.setdefault(name, {})
            for book, count in books.items():
                entry[book] = entry.get(book, 0) + count
    except TransportError as exc:
        print(f"✗ could not list markets: {exc}", file=sys.stderr)
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
    print(f"markets seen across the next {len(upcoming)} fixture(s):\n")
    for name, books in rows:
        offered = ", ".join(f"{book} ({count})" for book, count in sorted(books.items()))
        print(f"  {name.ljust(width)}  {offered}")
    print(f"\n{len(rows)} market(s). Put the names in MARKETS in your .env.")
    print("Matching is case-insensitive substring; prefix with - to exclude, e.g. Totals,-HT")
    return 0


def cmd_probe(config: Config) -> int:
    """Fetch odds for the next fixture and show exactly what came back.

    This is the diagnostic that answers the two questions `check` cannot: are
    the configured bookmaker names the ones this account actually receives
    prices for, and does the payload match what the parser expects. When
    nothing parses, the raw response is printed so the shape can be read.
    """
    import json

    from .odds_api import parse_quotes

    store, _, api, _ = _components(config)
    try:
        now = now_ts()
        events = api.get_events(config.sports[0])
        upcoming = sorted(
            (e for e in events if e.seconds_to_start(now) > 0), key=lambda e: e.start_ts
        )
        if not upcoming:
            print("no upcoming fixtures to probe", file=sys.stderr)
            return 1

        event = upcoming[0]
        print(f"probing: {event.name}  ({event.league or 'unknown league'})")
        print(f"         kick-off {format_clock(event.start_ts)}, event id {event.id}")
        print(f"         asking for bookmakers: {', '.join(config.bookmakers)}\n")

        payload = api.get_event_odds_raw(event.id, config.bookmakers)
        quotes = parse_quotes(payload, default_event_id=event.id)
    except TransportError as exc:
        print(f"✗ probe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    if not quotes:
        print("✗ no prices parsed from the response. Raw payload below:\n")
        print(json.dumps(payload, indent=2)[:4000])
        return 1

    by_book: dict = {}
    for quote in quotes:
        by_book.setdefault(quote.bookmaker, []).append(quote)
    print(f"✓ parsed {len(quotes)} price(s) from {len(by_book)} bookmaker(s)")
    for book, book_quotes in sorted(by_book.items()):
        print(f"\n  {book} — {len(book_quotes)} price(s)")
        for quote in book_quotes[:6]:
            print(f"    {quote.label}: {quote.odds:.2f}")

    wanted = {b.lower() for b in config.bookmakers}
    missing = wanted - set(by_book)
    if missing:
        print(f"\n! no prices for: {', '.join(sorted(missing))}")
        print("  Either the identifier is wrong (`bookmakers --search`) or this")
        print("  fixture simply has no market at that book — try again on a bigger game.")
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
    setup_logging(args.log_level or config.log_level)

    if args.command == "check":
        return cmd_check(config)
    if args.command == "select-bookmakers":
        return cmd_select_bookmakers(config)
    if args.command == "bookmakers":
        return cmd_bookmakers(config, args.search)
    if args.command == "leagues":
        return cmd_leagues(config, args.search)
    if args.command == "probe":
        return cmd_probe(config)
    if args.command == "markets":
        return cmd_markets(config, args.search)
    if args.command == "chat-id":
        return cmd_chat_id(config)
    if args.command == "status":
        return cmd_status(config)

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
