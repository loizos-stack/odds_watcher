# odds_watcher

A Telegram bot that messages you when a betting line **drops in the last 10
minutes before kick-off** at **bet365** or **betano**, using the
[odds-api.io](https://odds-api.io/) free tier.

```
📉 Odds drop · BET365

Ajax vs PSV
football · Eredivisie
Kick-off in 4m 12s (2026-08-22 19:00 UTC)

Market: moneyline · Home
Odds: 2.00 → 1.80 (-10.0%)
```

## How it decides to alert

"Line drops 0–10 minutes before game time" is implemented as:

1. From **45 minutes** before kick-off (`BASELINE_LEAD_SECONDS`) the bot records
   every price bet365 and betano offer on each upcoming fixture.
2. The price standing when the fixture crosses **T-10 minutes**
   (`WINDOW_START_SECONDS`) becomes the **baseline**.
3. Inside the window (T-10 → kick-off, `WINDOW_END_SECONDS`) every poll compares
   the current price to that baseline. A shortening of at least
   **`MIN_DROP_PCT` (default 5%)** fires a Telegram message.
4. After an alert the baseline resets to the alerted price, so a line that keeps
   sliding sends a follow-up only once it drops another full threshold — not on
   every poll.

Consequences worth knowing:

* A drop that happened an hour before kick-off is **not** reported; only
  movement inside the window counts. Widen the window with
  `WINDOW_START_SECONDS` if you want more.
* A fixture first seen inside the window has no baseline and is skipped — the
  bot must be running before T-10 for that game.
* "Drop" means the **price shortens** (2.00 → 1.80), which is the direction that
  signals money coming in. Prices drifting out are ignored.

## Setup

### 1. Get the two credentials

* **odds-api.io key** — sign up at <https://odds-api.io/#pricing> and copy the
  API key. The free tier gives 100 requests/hour (500/day) and lets you select
  **2 bookmakers**, which is exactly bet365 + betano.
* **Telegram bot** — message [@BotFather](https://t.me/BotFather), send
  `/newbot`, and copy the token it returns. Then send any message to your new
  bot so it is allowed to write to you.

### 2. Configure

```bash
git clone https://github.com/loizos-stack/odds_watcher.git
cd odds_watcher
cp .env.example .env
$EDITOR .env            # fill in ODDS_API_KEY and TELEGRAM_BOT_TOKEN
```

Find your chat id (after messaging the bot at least once) and put it in `.env`:

```bash
python -m odds_watcher chat-id
```

### 3. Bind the account to bet365 + betano and verify

```bash
python -m odds_watcher select-bookmakers   # uses BOOKMAKERS from .env
python -m odds_watcher check               # sends a test message, lists fixtures
```

### 4. Run

```bash
python -m odds_watcher run                 # daemon: polls and alerts
python -m odds_watcher run --dry-run       # detect and log, send nothing
python -m odds_watcher once                # single poll (for cron)
python -m odds_watcher status              # tracked lines + remaining budget
```

There are no runtime dependencies — Python 3.9+ standard library only.
`requirements.txt` only pulls in `pytest` for the test suite.

## Staying inside the free tier

100 requests/hour is not much, so the loop is frugal by design:

| Behaviour | Effect |
| --- | --- |
| Fixture list cached for `EVENTS_REFRESH_SECONDS` (15 min) | ~4 requests/hour for `/events` |
| Odds fetched only for fixtures inside `BASELINE_LEAD_SECONDS` | nothing spent on games hours away |
| Up to 20 fixtures per `/odds/multi` call | one request covers a whole slate |
| Adaptive sleep — fast near kick-off, idle otherwise | no polling when nothing is close |
| `RequestBudget` counter persisted in SQLite | hard stop at `MAX_REQUESTS_PER_HOUR` / `_PER_DAY`, across restarts |

If your account's plan does not expose `/odds/multi`, the client detects the
error once and falls back to one request per fixture — watch the budget then,
and consider raising `POLL_INTERVAL_SECONDS` or narrowing `LEAGUES`.

## Deployment

**systemd** (see `systemd/odds-watcher.service`):

```bash
sudo cp -r . /opt/odds_watcher
sudo cp systemd/odds-watcher.service /etc/systemd/system/
sudo systemctl enable --now odds-watcher
journalctl -u odds-watcher -f
```

**Docker**:

```bash
docker build -t odds-watcher .
docker run -d --name odds-watcher --env-file .env -v odds-data:/data odds-watcher
```

**cron** (poll every minute; state lives in the database between runs):

```cron
* * * * * cd /opt/odds_watcher && /usr/bin/python3 -m odds_watcher once >> /var/log/odds_watcher.log 2>&1
```

## Configuration reference

Every setting is an environment variable, documented in
[`.env.example`](.env.example). The ones you are most likely to change:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOOKMAKERS` | `bet365,betano` | Books to watch (free tier allows 2) |
| `SPORTS` | `football` | Comma-separated sports to follow |
| `LEAGUES` | *(all)* | Restrict to specific league slugs |
| `MARKETS` | *(all)* | e.g. `moneyline,spreads` |
| `WINDOW_START_SECONDS` | `600` | Window opens 10 min before kick-off |
| `WINDOW_END_SECONDS` | `0` | Window closes at kick-off |
| `BASELINE_LEAD_SECONDS` | `2700` | Start recording prices 45 min out |
| `MIN_DROP_PCT` | `5.0` | Minimum shortening to alert on |
| `POLL_INTERVAL_SECONDS` | `60` | Poll cadence while fixtures are close |
| `MAX_REQUESTS_PER_HOUR` | `90` | Local cap below the free tier's 100 |

## Layout

```
odds_watcher/
  config.py     environment parsing and validation
  http.py       stdlib HTTP with timeouts, retries, backoff
  odds_api.py   odds-api.io client + tolerant payload parsers
  store.py      SQLite: baselines, sent alerts, request budget
  detector.py   the drop rule (pure logic, no I/O)
  telegram.py   Bot API client and message formatting
  watcher.py    polling loop and scheduling
  cli.py        run / once / check / select-bookmakers / chat-id / status
tests/          53 unit tests, no network access required
```

## Tests

```bash
pip install -r requirements.txt
python -m pytest -q
```

## Notes

* Odds are decimal. Drop percentages are computed on the decimal price.
* All times in messages are UTC.
* Nothing here places bets or logs into a sportsbook; it only reads public odds
  data and sends you a message.
