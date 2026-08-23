# odds_watcher

A Telegram bot that messages you when a betting line **drops in the last 10
minutes before kick-off** at the two bookmakers you choose, using the
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

`BASELINE_MODE` picks between two rules:

| | `window-entry` (default) | `first-seen` |
| --- | --- | --- |
| Reference price | last price before the window opens | first price recorded for the line |
| When a signal can fire | only inside the alert window | any time from the start of tracking to kick-off |
| Tracked period set by | `WINDOW_START_SECONDS` | `BASELINE_LEAD_SECONDS` |
| A drop finishing before the window | ignored | signalled |

In both modes the reference resets to the signalled price after each alert, so
a line sliding 2.00 → 1.89 → 1.79 → 1.69 produces three separate signals rather
than one, and a slow drift produces none.

### window-entry, in detail

"Line drops 0–10 minutes before game time" is implemented as:

1. From **15 minutes** before kick-off (`BASELINE_LEAD_SECONDS`) the bot records
   every price the two watched bookmakers offer on each upcoming fixture.
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
  **2 bookmakers**, selected on the odds-api.io dashboard.
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

`.env` is gitignored, so pulling never adds newly introduced settings to it —
`status` lists any that exist in `.env.example` but not in your `.env`, whose
defaults are therefore silently in force.

Find your chat id (after messaging the bot at least once) and put it in `.env`:

```bash
python -m odds_watcher chat-id
```

### 3. Find the bookmaker identifiers and bind the account

The API uses its own identifiers, which are not always the obvious lowercase
name — list them first and put the left-hand column in `BOOKMAKERS`:

```bash
python -m odds_watcher bookmakers --search bet
python -m odds_watcher select-bookmakers   # uses BOOKMAKERS from .env
python -m odds_watcher check               # sends a test message, lists fixtures
```

`check` also reports how many fixtures sit inside the tracking lead right now
and whether that fits your hourly request budget — heed it, a wide slate will
exhaust the free tier.

If `check` shows no selected bookmakers or you are unsure the names are right,
`probe` fetches the next fixture's odds and prints exactly which books
answered, dumping the raw payload when nothing parses:

```bash
python -m odds_watcher probe
```

### 4. Run

```bash
python -m odds_watcher run                 # daemon: polls and alerts
python -m odds_watcher run --dry-run       # detect and log, send nothing
python -m odds_watcher once                # single poll (for cron)
python -m odds_watcher usage               # spend, account balance, burn rate
python -m odds_watcher status              # provider, budget, and any settings
                                           #   your .env is missing
python -m odds_watcher bookmakers          # valid bookmaker identifiers
python -m odds_watcher leagues --search x  # valid league identifiers
python -m odds_watcher sports --all        # every competition, in and out of season
python -m odds_watcher markets             # market names the books actually offer
python -m odds_watcher markets --discover  # probe prop keys (The Odds API)
python -m odds_watcher coverage            # per-league: which books price what
python -m odds_watcher props               # do props need per-fixture requests?
python -m odds_watcher probe               # show the prices actually returned
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

**Windows** — start a dry run at a set time, or register a scheduled task:

```powershell
.\scripts\dry-run.cmd 20:10            # waits in the console, then runs
.\scripts\schedule-dry-run.cmd 20:10   # survives the console closing
```

Use the `.cmd` wrappers: Windows blocks unsigned `.ps1` files by default, and
these invoke PowerShell with `-ExecutionPolicy Bypass` for that one call rather
than asking you to loosen the machine's policy. The `.ps1` files can be run
directly if your policy already permits it, or after:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

The machine has to be awake at that time; sleep delays the task rather than
running it.

**cron** (poll every minute; state lives in the database between runs):

```cron
* * * * * cd /opt/odds_watcher && /usr/bin/python3 -m odds_watcher once >> /var/log/odds_watcher.log 2>&1
```

## Configuration reference

Every setting is an environment variable, documented in
[`.env.example`](.env.example). The ones you are most likely to change:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOOKMAKERS` | `Bet365,DraftKings` | Books to watch (free tier allows 2) |
| `ODDS_PROVIDER` | `odds-api-io` | `odds-api-io` or `the-odds-api` |
| `SPORTS` | `baseball` | Sports to follow (`baseball_mlb` on The Odds API) |
| `REGIONS` | `us` | The Odds API only; multiplies credit cost |
| `PROP_MARKETS` | *(none)* | The Odds API only; charged per fixture |
| `LEAGUES` | *(all)* | Restrict to specific league slugs |
| `MARKETS` | *(all)* | e.g. `Totals,-F5`; substring, `-` excludes |
| `OUTCOMES` | *(all)* | e.g. `over,under` to watch totals only |
| `WINDOW_START_SECONDS` | `600` | Window opens 10 min before kick-off |
| `WINDOW_END_SECONDS` | `0` | Window closes at kick-off |
| `BASELINE_LEAD_SECONDS` | `900` | Start recording prices 15 min out |
| `MIN_DROP_PCT` | `5.0` | Minimum shortening to alert on |
| `MAX_ALERTS_PER_POLL` | `20` | Largest drops win when a poll finds many |
| `PER_EVENT_ODDS` | `false` | One request per fixture; needed for props |
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
  cli.py        run / once / check / probe / bookmakers / leagues /
                select-bookmakers / chat-id / status
tests/          72 unit tests, no network access required
```

## Tests

```bash
pip install -r requirements.txt
python -m pytest -q
```

## Finding market keys on The Odds API

That provider returns only the featured markets unless specific keys are named
in the request, and has no endpoint listing which keys exist — so sampling a
response can never reveal them. `markets --discover` requests the documented
keys for your sport against a live fixture and reports which ones your books
actually price:

```bash
python -m odds_watcher markets --discover
```

```
probing 28 market key(s) for baseball_mlb — costs up to 28 credit(s)

returning prices (7):
    batter_home_runs      draftkings (2)
    pitcher_strikeouts    draftkings (2)
    ...
rejected by the API (3):
    batter_stolen_bases, batter_triples, pitcher_record_a_win

PROP_MARKETS=batter_hits,batter_home_runs,...
```

A key the API does not recognise fails the entire request, so rejected keys are
isolated by retrying the chunk one key at a time rather than losing the batch.
"Accepted but empty" means the key is valid but unpriced for that fixture — try
a bigger game before ruling it out.

## Watching several competitions

`SPORTS` takes a list. On The Odds API each competition is its own sport key,
so soccer leagues are added there rather than through `LEAGUES`:

```bash
SPORTS=baseball_mlb,soccer_epl,soccer_spain_la_liga,soccer_italy_serie_a
```

Odds are fetched per sport — the endpoint is scoped that way and a mixed batch
returns nothing — so each competition costs its own featured call per poll
(3 credits x regions), plus its own props per fixture. Market keys are resolved
per sport too, and every `soccer_*` league shares one key set.

## Requesting everything

```bash
PROP_MARKETS=all
```

This asks for every market key catalogued for the sport. Because an
unrecognised key fails the whole request, the usable set is discovered once —
costing about one credit per candidate key — cached in the database, and
re-probed when `MARKET_KEYS_TTL_SECONDS` (default 24h) expires. The watcher
logs which set it is using on each start.

**The cost is severe.** With ~25 usable MLB keys, each key costs a credit per
fixture per poll:

| Setup | Credits/month | |
| --- | ---: | --- |
| 60s poll, 5 games in window, 1 region | 1,382,400 | ~7x the $99 plan |
| 120s poll, 5 games in window | 691,200 | ~4x |
| 300s poll, 5 games in window | 276,480 | ~2x |
| 300s poll, 2 games in window | 114,480 | fits Business |
| 60s poll, 5 games, 2 regions | 2,764,800 | ~14x |

That is 7,680 credits/hour at a 60s poll over five simultaneous games. The
local `MAX_REQUESTS_PER_HOUR` / `_PER_DAY` caps are enforced in credits and
will throttle the watcher before the account drains, so set them to match what
you are willing to spend.

## Player props

odds-api.io documents player props as being available one fixture at a time,
and their coverage as mainly US sports at US bookmakers. Whether the batched
`/odds/multi` response carries them differs by account, so measure rather than
assume:

```bash
python -m odds_watcher props
```

It fetches the same fixture through both endpoints and reports the difference:

```
  /odds/multi (batched)   :    3 market(s)
  /odds       (per fixture):    8 market(s)

only in the per-fixture response (5):
    Player Hits
    Player Home Runs
    ...
=> the per-fixture endpoint returns more. Set PER_EVENT_ODDS=true to
   collect these, at the cost of one request per fixture per poll.
```

`PER_EVENT_ODDS=true` makes the watcher request each fixture separately. That
is the only way to reach per-fixture-only markets, and it costs one request per
fixture per poll rather than one per twenty — on the free tier that is the
binding constraint, so the command prints the arithmetic for your own slate and
the poll interval that would fit.

## Watching everything, including player props

`MARKETS=` and `OUTCOMES=` empty means every market the API returns, player
props included. That is tens of thousands of prices per poll on a full slate,
so two things matter:

* **Writes are batched.** A poll commits once per fixture, not once per price.
  A 15-game MLB slate with full props (~40k prices) takes about 0.7s; with a
  commit per price the same poll took 29s.
* **Alerts are ranked and capped.** `MAX_ALERTS_PER_POLL` (default 20) keeps
  the largest drops and logs the rest, so a busy minute cannot flood the chat
  or trip Telegram's per-chat rate limit.

Props move faster and further than game lines, so expect `MIN_DROP_PCT` to need
raising once you see real volume — calibrate with `run --dry-run`.

## Choosing a provider

`ODDS_PROVIDER` selects the odds source. Everything downstream — drop
detection, storage, alerting — is identical either way.

| | `odds-api-io` | `the-odds-api` |
| --- | --- | --- |
| Base URL | `api2.odds-api.io/v3` | `api.the-odds-api.com/v4` |
| Metering | per request | **credits = markets × regions, per call** |
| Bookmakers | selected on the account (2 on free) | chosen per request, via `REGIONS`/`BOOKMAKERS` |
| Featured markets | all markets in one batched call | one call per sport (`h2h,spreads,totals`) |
| Player props | in the same payload | **per fixture only**, charged per fixture |
| Leagues | separate league slugs, `leagues` command | folded into the sport key; use `sports --all` |

### The credit arithmetic on The Odds API

Because props are charged per fixture, they dominate the bill. A month of MLB
evenings (15 games, 6h/night, 30 days):

| Configuration | Credits/month | Fits |
| --- | ---: | --- |
| Featured only, 60s poll, 1 region | 32,400 | Business ($99) |
| Featured only, 60s poll, 2 regions | 64,800 | Business |
| + 3 prop markets, 5 games in window, 60s | 194,400 | Business, barely |
| + 3 prop markets, 5 games in window, 120s | 97,200 | Business |
| + 6 prop markets, 5 games in window, 60s | 356,400 | **over the $99 plan** |

Pro ($29) is 20,000 credits/month and excludes props entirely, so even
featured-only polling at 60s does not fit it. Every extra region multiplies
everything: `REGIONS=us,uk` doubles the bill.

`MAX_REQUESTS_PER_HOUR` / `_PER_DAY` are enforced locally in credits for this
provider, and `check` prints the account balance from the
`x-requests-remaining` header.

## Coverage comes first

A worldwide sport returns thousands of fixtures, and most of them are in
leagues no major bookmaker quotes — an unpriced fixture simply returns
`"bookmakers": {}`. Before tuning anything, find out what your books actually
price:

```bash
python -m odds_watcher coverage
```

```
  league                        sampled  bet365       draftkings
  England - Premier League      15       15           15
  Brazil - Serie A              15       15           0
  Brazil - Goiano, 2. Divisao   15       0            0

leagues priced by every watched book — a good starting LEAGUES:

LEAGUES=england-premier-league
```

`coverage` and `markets` sample *across* the upcoming slate rather than taking
the next few kick-offs, because which leagues are imminent depends entirely on
the hour of day — a feed queried at 21:00 UTC is all South America. `probe`
deliberately does the opposite and looks at what is about to start.

## Watching totals

Totals markets need two filters, because market naming varies and a market
matched by name may still carry rows you do not want:

```bash
MARKETS=Totals,Corner,Booking,Card,-HT,-Handicap,-First
OUTCOMES=over,under
```

`MARKETS` selects by substring and `-` excludes, so this keeps `Totals`,
`Corners Over/Under` and `Bookings Over/Under` while dropping `Totals HT`,
`Corners Handicap` and `First Corner`. `OUTCOMES` then keeps only the over and
under prices. Run `python -m odds_watcher markets` first — it samples the next
ten fixtures in one request and prints every market name with the books that
priced it, since corners and bookings markets often exist only on bigger games.

Each handicap line is tracked separately, so Over 2.5 and Over 3.5 never share
a baseline.

## The odds payload

`/odds` returns markets as a list per bookmaker, with prices in an `odds` array
of rows — the handicap on the row, prices as strings:

```json
"bookmakers": {
  "Bet365": [
    {"name": "ML",     "updatedAt": "...", "odds": [{"home": "1.420", "draw": "3.900", "away": "6.500"}]},
    {"name": "Spread", "updatedAt": "...", "odds": [{"hdp": -1.25, "home": "2.000", "away": "1.800"}]}
  ]
}
```

Each row is one line of the market, so two rows of `Alternative Asian Handicap`
are tracked as separate lines rather than colliding. A captured response lives in
`tests/fixtures/live_odds_response.json` and is asserted against directly.

## Notes

* Odds are decimal. Drop percentages are computed on the decimal price.
* All times in messages are UTC.
* Nothing here places bets or logs into a sportsbook; it only reads public odds
  data and sends you a message.
