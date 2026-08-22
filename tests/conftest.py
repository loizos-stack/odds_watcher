import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from odds_watcher.config import Config  # noqa: E402
from odds_watcher.store import Store  # noqa: E402


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        odds_api_key="key",
        telegram_bot_token="token",
        telegram_chat_id="42",
        bookmakers=("bet365", "betano"),
        sports=("football",),
        window_start_seconds=600,
        window_end_seconds=0,
        baseline_lead_seconds=2700,
        min_drop_pct=5.0,
        db_path=tmp_path / "test.db",
    )


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "state.db")
    yield store
    store.close()
