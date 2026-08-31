#!/usr/bin/env python3
"""Print the raw fixture payload ParlayAPI returns, to inspect start-time fields.

Some fixtures come back on a round placeholder start time (e.g. every game at
exactly 19:00:00 UTC) rather than their real first pitch. To filter those out
reliably we need to see what the API actually sends for them -- a status flag,
a second time field, whatever distinguishes a placeholder from a scheduled
game. This dumps the raw dicts, redacting nothing sensitive (fixtures carry no
secrets), so paste the output freely.

    cd /opt/odds_watcher && python3 -m scripts.dump_events            # baseball
    cd /opt/odds_watcher && python3 -m scripts.dump_events baseball 6
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from odds_watcher.cli import _components
from odds_watcher.config import Config, load_dotenv


def main(argv: list[str]) -> int:
    load_dotenv(Path(".env"))
    config = Config.from_env(required=(), env_file=Path(".env"))
    sport = argv[1] if len(argv) > 1 else (config.sports[0] if config.sports else "baseball")
    limit = int(argv[2]) if len(argv) > 2 else 5

    if config.odds_provider != "parlay-api":
        print(f"this dump only supports parlay-api; ODDS_PROVIDER={config.odds_provider}")
        return 2

    store, _budget, api, _tg = _components(config)
    try:
        from odds_watcher.parlayapi import _as_list
        payload = api._call(f"v1/sports/{sport}/events")
        raw = _as_list(payload)
    finally:
        store.close()

    print(f"{len(raw)} raw {sport} fixture(s); showing up to {limit}\n")
    # Show the union of keys once, then a few full examples.
    keys: set = set()
    for item in raw:
        if isinstance(item, dict):
            keys.update(item.keys())
    print("keys seen across fixtures:", ", ".join(sorted(keys)), "\n")

    for item in raw[:limit]:
        print(json.dumps(item, indent=2, ensure_ascii=False, sort_keys=True))
        print("-" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
