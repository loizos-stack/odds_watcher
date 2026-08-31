#!/usr/bin/env python3
"""Explain, from live data, when the watcher will alert and what time it shows.

A drop can look like it fires "too early" for two unrelated reasons:

  * the alert timing knobs (window / lead / baseline mode) let it fire further
    from first pitch than you expected, or
  * the *displayed* start time is in the wrong zone (DISPLAY_TIMEZONE), so a
    perfectly-timed alert reads as a game that "isn't on".

This prints both, per upcoming fixture, so the two are easy to tell apart.
Read-only: it fetches fixtures and reads config, it changes nothing.

    cd /opt/odds_watcher && python -m scripts.why_alert            # baseball
    cd /opt/odds_watcher && python -m scripts.why_alert baseball
"""

from __future__ import annotations

import sys
from pathlib import Path

from odds_watcher.cli import _components
from odds_watcher.config import Config
from odds_watcher.util import format_clock, now_ts


def classify(cfg: Config, seconds: float) -> str:
    """Where a fixture sits relative to this config's tracking and alert windows."""
    if seconds < cfg.window_end_seconds:
        return "window closed (past T-%dm)" % (cfg.window_end_seconds // 60)
    if seconds > cfg.baseline_lead_seconds:
        return "not tracked yet (before T-%dm)" % (cfg.baseline_lead_seconds // 60)
    # Inside the tracking lead. Whether it can ALERT depends on the mode.
    if cfg.baseline_mode == "first-seen":
        alerting = seconds <= cfg.baseline_lead_seconds  # whole lead is alertable
    else:
        alerting = seconds <= cfg.window_start_seconds
    return "ALERT WINDOW" if alerting else "tracking (no alerts until T-%dm)" % (
        cfg.window_start_seconds // 60
    )


def main(argv: list[str]) -> int:
    env_file = Path(".env")
    config = Config.from_env(required=(), env_file=env_file)
    sport = argv[1] if len(argv) > 1 else (config.sports[0] if config.sports else "baseball")

    print(f"baseline_mode         = {config.baseline_mode}")
    print(f"baseline_lead_seconds = {config.baseline_lead_seconds}  (tracking starts T-{config.baseline_lead_seconds // 60}m)")
    print(f"window_start_seconds  = {config.window_start_seconds}  (alerts begin  T-{config.window_start_seconds // 60}m)")
    print(f"window_end_seconds    = {config.window_end_seconds}  (alerts end    T-{config.window_end_seconds // 60}m)")
    print(f"display_timezone      = {config.display_timezone}")
    print()

    store, budget, api, _telegram = _components(config)
    try:
        now = now_ts()
        events = [e for e in api.get_events(sport) if e.seconds_to_start(now) > 0]
        events.sort(key=lambda e: e.start_ts)
    finally:
        store.close()

    if not events:
        print(f"no upcoming {sport} fixtures right now")
        return 0

    tz = config.display_timezone
    print(f"{'fixture':<34}  {'start (UTC)':<20}  {'start (' + tz + ')':<24}  {'T-minus':>8}  state")
    for e in events:
        seconds = e.seconds_to_start(now)
        mins = seconds / 60.0
        print(
            f"{e.name[:34]:<34}  "
            f"{format_clock(e.start_ts, 'UTC'):<20}  "
            f"{format_clock(e.start_ts, tz):<24}  "
            f"{mins:>7.0f}m  "
            f"{classify(config, seconds)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
