"""Small shared helpers (time parsing / formatting)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def now_ts() -> float:
    """Current UTC time as a POSIX timestamp."""
    return datetime.now(timezone.utc).timestamp()


def parse_timestamp(value: Any) -> Optional[float]:
    """Best-effort conversion of an API timestamp into POSIX seconds.

    Accepts epoch seconds, epoch milliseconds and ISO-8601 strings (with or
    without a trailing `Z`). Returns None when the value cannot be read.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Anything past ~year 2286 in seconds is really milliseconds.
        return float(value) / 1000.0 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_timestamp(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def format_clock(ts: float) -> str:
    """UTC wall clock, for message bodies and logs."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_countdown(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 0:
        return "in progress"
    minutes, secs = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"
