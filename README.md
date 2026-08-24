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

`BASELINE_MODE` picks between three rules:

| | `window-entry` (default) | `first-seen` | `last-seen` |
| --- | --- | --- | --- |
| Reference price | last price before the window opens | first price recorded for the line | the previous recorded price |
| When a signal can fire | only inside the alert window | any time from the start of tracking to kick-off | only inside the alert window |
| Tracked period set by | `WINDOW_START_SECONDS` | `BASELINE_LEAD_SECONDS` | `WINDOW_START_SECONDS` |
| A drop finishing before the window | ignored | signalled | ignored |
| A slow grind of small steps | signalled once it totals the threshold | signalled | **never** — each step is below it |

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
   **`MIN_DROP_PCT` (default 2%)** fires a Telegram message.
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

**A VPS** — the useful shape for this, since a laptop that sleeps misses
kick-offs. Any 1 GB box will do; the watcher is stdlib-only, so there is
nothing to install but Python.

```bash
# as root, on a fresh Debian/Ubuntu box
adduser --system --group --home /opt/odds_watcher odds
git clone <your fork> /opt/odds_watcher
install -d -o odds -g odds /var/lib/odds-watcher

cp /opt/odds_watcher/.env.focused.example /opt/odds_watcher/.env
$EDITOR /opt/odds_watcher/.env          # the three secrets, and DB_PATH
chown odds:odds /opt/odds_watcher/.env
chmod 600 /opt/odds_watcher/.env        # it holds your API key and bot token

cp /opt/odds_watcher/systemd/odds-watcher.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now odds-watcher
journalctl -u odds-watcher -f
```

Set the box's clock to UTC and leave it there — every window in this tool is
computed from kick-off timestamps, so a drifting clock silently shifts the
alert window. `timedatectl set-ntp true` is enough.

Note what a VPS does and does not buy. It buys uptime: the watcher is awake
for every kick-off instead of only when a laptop happens to be open. It does
not buy credits, and credits are the binding constraint on a free tier — an
unattended `SPORTS=all` will spend a month's allowance in an afternoon. Size
the scope first (below), then move it to a server.


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

Logs go to stdout, so `... | Tee-Object -FilePath watcher.log` in PowerShell
records them without PowerShell reporting healthy output as `NativeCommandError`.

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
| `BASELINE_LEAD_SECONDS` | `1200` | Start recording prices 20 min out |
| `MIN_DROP_PCT` | `2.0` | Minimum shortening to alert on |
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

## What a free tier can actually watch

Credits are not spent per game. One request returns every fixture for a sport,
so six matches kicking off at 15:00 cost exactly as much as one. What
multiplies is **how often you look**, and **how many sports you look at**:

```
credits per kickoff cluster  =  BASELINE_LEAD_SECONDS / POLL_INTERVAL_SECONDS
credits per day (idle)       =  sports x 86400 / EVENTS_REFRESH_SECONDS
```

At a 60s poll over a 20-minute lead that is 20 credits per cluster. On
ParlayAPI's 1,000/month free tier:

| Scope | Fixture refresh | Odds | 1,000 credits buy |
| --- | ---: | ---: | --- |
| 1 sport, 60s poll, 6h refresh | 120/mo | 20/cluster | **~44 clusters a month** |
| 1 sport, 300s poll, 6h refresh | 120/mo | 4/cluster | ~220 clusters, 4 samples each |
| 95 sports, 300s poll, 1h refresh | 68,400/mo | — | **4 hours, then nothing** |

The last row is the trap, and it fails in a way that looks like a quiet
market rather than a budget problem: with 95 sports something is always inside
the tracking lead, so the watcher never idles, and the allowance goes on
visiting thousands of lines **once each**. A line priced once cannot move, so
no threshold — 10%, 2%, 0.1% — will ever signal on it.

Movement needs at least two samples of the same line before kick-off. Spending
the budget on breadth buys one sample of everything; spending it on depth buys
twenty samples of one slate. Only the second can produce an alert.

`python -m odds_watcher movements` names which of the two you got:

```
1,412 tracked line(s): 0 fell, 0 rose, 1,412 unchanged
1,412 of 1,412 line(s) were priced only once, so they could not move
```

`.env.focused.example` is the depth configuration; `.env.all-sports.example`
is breadth, for an account that can afford it.

## Watching every sport

`SPORTS=all` expands to whatever the provider lists, refreshed daily — 89
sports on ParlayAPI. The cost has two halves, and it matters which is which:

* **The fixture refresh** asks every sport for its schedule, so it costs one
  request per configured sport each time the list goes stale
  (`EVENTS_REFRESH_SECONDS`). This is the half `SPORTS=all` multiplies.
* **Odds** are only requested for sports that have a fixture inside the
  tracking lead *at that moment* — one request each, however many sports are
  configured. Watching 89 sports when eight have a game starting soon costs
  eight requests, not 89. On ParlayAPI one call returns every fixture for the
  sport, so it stays one request no matter how many games are in range.

So a wide `SPORTS` list is paid for at refresh time, and a short
`POLL_INTERVAL_SECONDS` is paid for at poll time. Against ParlayAPI's
1,000/month free tier, with 89 sports listed and ~8 of them in range on a busy
evening:

| Poll / refresh | Odds per hour | Refresh per hour | Total/hour | 1,000 credits last |
| --- | ---: | ---: | ---: | --- |
| 60s / 900s | 480 | 356 | 836 | 1.2 h |
| 300s / 900s | 96 | 356 | 452 | 2.2 h |
| 300s / 3600s | 96 | 89 | **185** | **5.4 h** |
| 600s / 3600s | 48 | 89 | 137 | 7.3 h |
| 300s / 3600s, + all props | 192 | 89 | 281 | 3.6 h |

Raising `EVENTS_REFRESH_SECONDS` is the single biggest saving under
`SPORTS=all`; kick-off times do not move, so an hourly refresh loses nothing.
`.env.all-sports.example` is that third row, ready to copy.

The watcher prints its own estimate on the first poll, counting the sports
actually in range rather than the ones configured:

```
cost estimate: 8 request(s) per poll (odds, 8 sport(s) in range) every 300s
= ~96/hour, plus 89 per fixture refresh (89 sport(s)) every 3600s = ~89/hour;
~185 request(s)/hour in total
provider balance 858 lasts about 4.6 hour(s) at that rate
```

`MAX_REQUESTS_PER_HOUR` / `_PER_DAY` are the guardrail: the watcher throttles
itself when they are reached rather than draining the account, and `usage`
reports the burn rate against the balance.

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

## Enabling totals and props on ParlayAPI

`FEATURED_MARKETS` is sent as the `markets` query parameter. Without it the API
answers with `h2h` alone, so spreads and totals need it set explicitly:

```bash
FEATURED_MARKETS=h2h,spreads,totals
```

Props come from a separate `/v1/sports/{sport}/props` endpoint, enabled by
setting `PROP_MARKETS`, and cost one extra request per sport per poll:

```bash
PROP_MARKETS=all                      # every prop market the sport offers
PROP_MARKETS=player_strikeouts,player_total_bases   # or a chosen few
```

List what a sport offers — this provider publishes the keys, so no probing is
needed:

```bash
python -m odds_watcher markets --discover
```

Props arrive as flat rows (`player_name`, `line`, `over_price`, `under_price`)
rather than nested markets, and are converted into one tracked line per player,
side and book.

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

| | `odds-api-io` | `the-odds-api` | `parlay-api` |
| --- | --- | --- | --- |
| Base URL | `api2.odds-api.io/v3` | `api.the-odds-api.com/v4` | `parlay-api.com/v1` |
| Auth | `apiKey` query param | `api_key` query param | `X-API-Key` header |
| Metering | per request | **credits = markets × regions, per call** | per request |
| Cost of one poll | 1 per 20 fixtures | 3+ per sport, props per fixture | **1 per sport** |
| Price format | decimal | decimal | American, converted on the way in |
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

## When nothing signals

Silence has two very different causes: the prices did not move, or they moved
less than `MIN_DROP_PCT`. The alert log cannot tell them apart, so the recorded
prices are reported directly:

```bash
python -m odds_watcher movements
```

```
9 tracked line(s): 6 fell, 1 rose, 2 unchanged
largest drop recorded: 5.33%   (your threshold: 10.0%)

  fixture                book         market   outcome      from      to     move
  Dodgers vs Padres      bet365       h2h      Dodgers      1.50    1.42   -5.33%
  Phillies vs Cardinals  fanduel      h2h      Phillies     1.77    1.69   -4.52%
  ...
! nothing moved as far as 10.0%. The largest drop was 5.33%,
  so MIN_DROP_PCT=3.2 would have signalled the sharpest moves.
```

Moneylines at major books move in low single digits in the final minutes; a
10% shortening is a rarity there, whatever it looks like on longer-priced
markets. Set the threshold from this report rather than from intuition.

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
