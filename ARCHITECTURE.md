# Architecture

## Deployment shape: shared loop, not one process per customer

**Recommendation: a single process running one shared `asyncio` event loop
that iterates every active customer account each poll cycle, using
`ccxt.async_support` for concurrent exchange I/O.** Not one OS process per
customer.

Reasoning:

- **Resource cost scales with customers either way, but processes cost
  more per customer.** A Python interpreter has real baseline overhead
  (tens of MB of RSS, its own import of ccxt/SQLAlchemy/etc, its own file
  descriptors and OS scheduling slot). A resale product wants adding a
  customer to be cheap. `asyncio` tasks cost kilobytes, not megabytes.
- **The workload is I/O-bound, not CPU-bound.** Every cycle is "wait on a
  network call to Binance, run some arithmetic, maybe place an order."
  That's exactly what an event loop is for - one process can hold
  hundreds of concurrent exchange connections without threads.
- **Binance rate limits are per API key / per IP, and easier to respect
  from one place.** ccxt's `enableRateLimit` throttles per-client; a
  single process can additionally apply a global cap across all accounts
  hitting the same IP. Independent processes can't coordinate that
  without extra infrastructure (Redis, a semaphore service, etc.) and are
  more likely to collectively trip a rate limit or an IP ban.
- **One process is much simpler to run and watch.** One systemd unit, one
  log stream, one health check, one thing to restart on deploy. N
  processes means a supervisor to spawn/monitor/restart N things and
  aggregate N log streams - real operational cost for a single-server
  deployment with no orchestration layer.

The trade-off this gives up is process-level fault isolation: a truly
unhandled exception in one customer's cycle could, in theory, take down
the whole loop. This is mitigated directly rather than by paying for N
processes:

- `trader.process_account` wraps each account's cycle; `scheduler._run_one`
  wraps that again and logs-and-continues on any exception.
- `scheduler.run_once` fans out over all accounts with
  `asyncio.gather(..., return_exceptions=True)`, so one account raising
  never cancels the others' tasks or stops the poll cycle.
- Each account cycle opens its own DB session and ccxt client and closes
  them at the end of the cycle - no shared mutable state between accounts
  within a poll.

**When to reconsider:** if customer count grows to where one event loop's
per-cycle wall time starts exceeding `POLL_INTERVAL_SECONDS`, shard
accounts across a handful of worker processes (`account_id`'s hash mod N)
rather than jumping to one-per-customer. That's a small change to
`scheduler.run_once` (filter the account list by shard) and gets most of
the throughput benefit of more processes without the per-customer
overhead cost.

## Paper trading: one shared testnet key, per-customer simulated ledger

The spec calls for "Binance testnet, using simulated funds." Binance
testnet accounts are a separate namespace from mainnet - a customer's
mainnet key can't be used against testnet endpoints, and asking every
signup to also go generate a personal testnet key is friction a resale
product shouldn't add.

So: the platform holds **two** Binance testnet keys, one per market type
(`TESTNET_SPOT_API_KEY`/`TESTNET_SPOT_API_SECRET`,
`TESTNET_FUTURES_API_KEY`/`TESTNET_FUTURES_API_SECRET`) - not one shared
pair. Binance's spot testnet (testnet.binance.vision) and futures
testnet (testnet.binancefuture.com) are separate systems with separate
key registrations; a key issued on one is not valid on the other, so
`trader._resolve_credentials` picks the pair matching `account.market_type`.
What's customer-specific isn't the exchange credential, it's the
simulated ledger - `TradingAccount.paper_balance` tracks each account's
own equity curve independently in our database, starting from
`PAPER_STARTING_BALANCE`. Orders during the trial aren't actually sent to
the exchange at all (see `trader._maybe_enter`/`_maybe_exit`: `is_paper`
skips `client.create_market_order` and only writes a `Trade` row) - only
market data (OHLCV) is fetched from testnet, and P&L is computed locally
against `paper_balance`. This keeps paper accounts free of any real
funds or exchange-side state, and makes "how much would I have made"
purely a property of our own records.

The customer's own key (`ApiCredential`, encrypted at rest) is collected
at signup, tagged with an explicit `environment` ("testnet" | "mainnet" |
"unknown") the customer chooses - never inferred - and stays unused for
trading either way until the account reaches LIVE, which requires far
more than trial expiry or payment alone. See "Live trading: four
independent gates, not one" below for why, and for the real incident
that made this its own section.

## Web API and frontend: one process still does the trading

The self-serve product (website signup, Telegram linking, billing) adds
a FastAPI app (`src/mba_bot/api/`) and a React frontend (`frontend/`) on
top of the trading engine above, without changing how the engine itself
runs. The key decision carried over from the shared-loop section: the
trading scheduler is started as a background `asyncio` task inside
FastAPI's lifespan hook (`api/app.py`), in the *same* process that
serves HTTP. There's still exactly one process to deploy on the VPS -
it just now also answers requests instead of only looping silently.

- **Auth** is email+password (bcrypt) with a JWT in an httpOnly,
  `SameSite=None; Secure` cookie (`api/routers/auth.py`) - no token for
  the frontend to manage itself, no OAuth integration for v1.
  `SameSite=None` specifically because the frontend (Vercel) and backend
  (this API) are on different domains, making every request between them
  cross-site; `SameSite=Lax` is never sent on a cross-site fetch/XHR, only
  on a top-level link navigation, so Lax silently breaks auth the moment
  the two are split across domains like this.
- **Onboarding as HTTP** (`api/routers/account.py`) calls the exact same
  `onboarding.start_trial`/`accept_live_bypass` and `crypto_utils.encrypt`
  functions the CLI script (`scripts/signup_customer.py`) always used -
  there is one implementation of "what happens at signup", reachable two
  ways.
- **Telegram linking** (`api/routers/telegram.py`) exists because a
  customer can't type in their own numeric chat id: a one-time
  `telegram_link_token` on the account becomes a deep link
  (`t.me/<bot>?start=<token>`); Telegram relays the resulting `/start`
  message to our webhook, which resolves the token to a `chat_id` and
  fills in `notification_target`. `trader.process_account` won't trade an
  account with no `notification_target` set, so this is a real gate, not
  just UX polish.
- **Billing and the lock, by design** (`api/routers/billing.py`): the
  free trial is unlimited; payment buys *entitlement* to live trading,
  never live trading itself - see the next bullet for why that's now a
  hard separation, not a one-flag gate. A new account's
  `TradingAccount.is_active` defaults `True`. There is no customer-facing
  Pause/Resume control any more (`AccountUpdateRequest` no longer accepts
  `is_active` - `api/routers/account.py`'s `update_account` removed it
  entirely); it is an internal/admin-only kill switch now.
  `db.list_active_accounts` - the only thing the scheduler calls to
  decide what to run this tick - checks that OR whether the account has
  a currently open trade: an open position must never be orphaned by the
  kill switch (no monitoring, no exit, no stop-loss, forever) just
  because `is_active` was flipped off internally. It does not check
  `has_paid` at all, by design (see the next bullet for what does).

  `has_paid` flips exactly two ways, both ending in the same place:
  - Paystack's hosted payment page (card data never touches this server)
    for a one-time charge, confirmed by a webhook that verifies
    Paystack's own signature (HMAC-SHA512 over the raw body, keyed by the
    same secret key used for API calls - no separate webhook secret like
    Stripe has). `apply_payment_event` is split from that
    signature-verification code so it's unit-testable with a hand-built
    event dict - see `tests/test_api_billing.py`. It only ever touches
    `has_paid` and `TradingAccount.is_active`; it has no code path that
    can set `live_trading_enabled_at` or an `ApiCredential.environment` -
    see `tests/test_api_live_trading_safety.py::test_payment_webhook_never_sets_live_trading_fields`.
  - `scripts/mark_customer_paid.py`, run by the operator over SSH, for
    anyone who paid outside Paystack (bank transfer, cash, a comp). Same
    flag, same effect - the scheduler can't tell the difference and isn't
    meant to.

  Lifetime access, not a subscription: once `has_paid` is set it never
  gets cleared by anything in this codebase.

- **Live trading: five independent gates, not one** (`onboarding.resolve_trading_mode`):
  this exists because of a real incident - an account reached LIVE
  carrying a Binance *testnet* key, and production correctly rejected
  it, because back then `has_paid` plus trial expiry/bypass alone was
  sufficient to flip `is_paper` to `False`, and `_resolve_credentials`
  just handed over whatever credential was on file with no idea whether
  it was even valid for the environment it was about to hit.
  `resolve_trading_mode` now checks five conditions, in order, and fails
  closed to PAPER the moment any one of them doesn't hold:

  1. `has_paid` - buys the *right* to use live trading, nothing more.
  2. Eligibility (`is_live_eligible`) - the trial ran its course, or the
     customer accepted the bypass to skip waiting. Still just eligibility.
  3. `TradingAccount.live_trading_enabled_at` is set - a dedicated,
     separate action (`POST /api/account/enable-live-trading`) distinct
     from both of the above. Nothing in billing.py or the trial-expiry
     check can ever set this; the endpoint itself refuses unless 1 and 2
     already hold, a mainnet-tagged credential is on file, and there's no
     open trade (switching environment mid-trade is exactly as unsafe as
     switching pair/market mid-trade, see below).
  4. `TradingAccount.has_mainnet_credential` (a model property: the
     account's `ApiCredential.environment == "mainnet"`) - re-checked
     every single scheduler cycle, not just at the moment live trading
     was enabled. If the stored credential is later downgraded to
     testnet or replaced, this alone makes LIVE fail closed again on the
     account's very next cycle. `api/routers/account.py`'s credential
     endpoint also explicitly clears `live_trading_enabled_at` (and
     `bot_started_at`, condition 5 below) on a downgrade, as a second,
     independent safeguard - but even without that bookkeeping,
     condition 4 alone is enough to stop it.
  5. `TradingAccount.bot_started_at` is set - the explicit, separate
     "Start Bot" action (`POST /api/account/start-bot`), distinct from
     enabling live trading. Accepting the risk disclosure authorizes
     going live; it does not start anything. The endpoint re-checks 1-4
     plus `allocated_capital is not None` and no open trade before
     setting this. This column shipped after four-gate accounts were
     already live in production, so it is `NULL` for every pre-existing
     row - every one of them fails closed to PAPER the moment the
     migration runs, until its owner explicitly presses Start Bot. No
     Start consent is ever fabricated for an existing account.

  A pre-existing account can have an already-open LIVE trade at the
  moment this gate ships (its `bot_started_at` is `NULL`, so new-entry
  mode now resolves to PAPER) - that must never mean the open position
  stops being managed with real credentials. `trader.process_account`
  resolves two different things, deliberately: `is_paper` (fresh every
  cycle, from the five gates above, decides whether a *new* entry may go
  live) and `effective_is_paper` (`open_trade.is_paper` when a trade is
  already open, else `is_paper`) - credentials, the exchange client's
  sandbox flag, and `_maybe_exit`'s real-vs-paper order decision all key
  off `effective_is_paper`, never the freshly recomputed gate result. See
  `tests/test_capital_lifecycle_and_auto_select_safety.py`'s
  `test_open_live_trade_keeps_being_managed_with_real_credentials_even_if_the_account_is_newly_forced_to_paper`
  for the regression this exists to prevent.

  `ApiCredential.environment` ("testnet" | "mainnet" | "unknown") is set
  by explicit customer choice at signup and updatable afterward via
  `PUT /api/account/credentials` - never inferred from the key's content,
  since there's no reliable way to tell a testnet key from a mainnet one
  by looking at it. "unknown" is what every credential that predates
  this concept was migrated to (see the migration that added this
  column) - deliberately not "testnet" or "mainnet", since a credential
  nobody has confirmed must never be treated as authorized for either,
  and specifically can never satisfy condition 4 above.
- **Legal consent is a fifth, additive gate - never a substitute for any
  of the four above** (`legal.py`, `api/deps.require_legal_acceptance`,
  `models.LegalAcceptance`): every customer must affirmatively accept
  the Terms of Service and acknowledge the Privacy Policy at signup
  (`SignupRequest.accept_terms`/`acknowledge_privacy`, both required
  `True` or the request 422s before any row is written), and every
  `/api/account/*` route re-checks that the customer's acceptance is
  still current before doing anything else - including create-account,
  so there's no way to reach trading functionality without it. A
  customer who signed up before these documents existed simply has zero
  `LegalAcceptance` rows and fails this exactly like anyone else; nothing
  backfills history for them - they catch up once via
  `POST /api/auth/accept-legal`. Enabling live trading additionally
  requires `EnableLiveTradingRequest.accept_risk_disclosure = True`,
  recorded as its own `LegalAcceptance` row alongside
  `live_trading_enabled_at` in the same request/transaction as the
  gate check above - a failed gate check never leaves a
  risk-disclosure row behind, since every gate is checked before any
  write. `LegalAcceptance` stores only `(customer_id, document_type,
  version, accepted_at)` - no credentials, ever - and a unique
  constraint on `(customer_id, document_type, version)` stops the same
  acceptance from ever being recorded twice; a new document version is
  simply a new row, not an update to an old one.
- **Pair/market switching, and why it's safe to do mid-lifetime**
  (`api/routers/account.py`'s `update_account`): `has_paid` unlocks the
  account, not a pair - `PATCH /api/account` accepts `pair`/`market_type`
  at any time, for paid and unpaid customers alike, validated against the
  same `SUPPORTED_PAIRS`/`SUPPORTED_MARKET_TYPES` creation uses. The one
  thing it refuses is switching while `db.get_open_trade` finds an open
  position: `trader.py` always closes using the account's *current*
  `pair`/`market_type`, not whatever the trade was opened under (nothing
  per-trade records that), so a switch mid-trade would have the next
  cycle try to exit through the wrong symbol - `409` instead. A real
  switch also clears the runtime-status fields below, since a reading
  from the old pair has nothing to say about the new one. The same
  open-trade guard now also covers `pair_selection_mode` (below).
- **Trading Capital, and why it is a ceiling, never a transfer**
  (`TradingAccount.allocated_capital`, `risk.py`, `trader.py`'s
  `_maybe_enter`): the customer's funds never leave their own Binance
  account - this column is the maximum the bot's position sizing may
  treat as available. `None` means "not configured" and must never be
  read as "use everything available" - a live entry with
  `allocated_capital is None` is refused outright (defense in depth:
  the Start Bot endpoint already can't be reached without it, but
  `_maybe_enter` checks again independently). When it is set,
  `effective_capital = min(allocated_capital, live Binance balance)` -
  the real balance is still read fresh from Binance at order time, so a
  customer who allocates more than they actually have is capped by
  reality, never by a stale number. `risk.compute_position_size`'s
  `risk_amount = effective_capital * risk_pct / 100` formula is
  unchanged; only what "effective_capital" means changed. Two
  independent ceilings trim the resulting quantity *down*, never up,
  after that: `risk.apply_exposure_ceiling` caps notional
  (`quantity * entry_price`) at `effective_capital * max_notional_multiplier`
  (`Settings.max_notional_multiplier`, operator-configured, a no-op for
  Spot at its natural 1.0x) because a tight ATR stop alone can size an
  unbounded notional for the same dollar risk; `risk.apply_margin_ceiling`
  (Futures only) additionally caps so `notional / leverage <= effective_capital`
  using the account's real, currently-configured leverage, read via
  `exchange.fetch_symbol_leverage` - a plain read-only GET
  (`fetch_leverage`/`fetch_leverages` in ccxt). This module never calls
  `set_leverage`: reading ccxt's own source shows it has real,
  account-wide, cross-position side effects (margin mode, liquidation
  price), which a trading bot must never trigger silently on a
  customer's behalf. A leverage read that fails, returns nothing, or
  returns a non-positive value (`fetch_symbol_leverage` collapses all
  three to `None` - it never guesses a value) makes `_maybe_enter` fail
  closed for that one Futures entry: no order is placed, and a short,
  customer-safe operational error is recorded instead (same
  `_record_error` path a data-fetch failure uses), because a margin
  check that can't be proven to hold is treated the same as a margin
  check that failed. This is scoped to a brand-new live Futures entry
  only - it never affects Spot (no leverage concept there), and never
  affects managing an already-open position (that's
  `effective_is_paper`/`open_trade`'s domain in `process_account`, not
  `_maybe_enter`'s). The independent notional ceiling above still
  applies regardless of any of this.
- **Auto Select pair ranking, and why pairs never move mid-trade**
  (`pair_selection.py`, `trader.py`'s `_scan_auto_pair`):
  `TradingAccount.pair_selection_mode` (`"manual"` default | `"auto"`)
  lets the bot pick its own pair between trades, from the same fixed,
  already-maintained `config.SUPPORTED_PAIRS` universe manual selection
  uses - not a second, separately-curated list, and not "every Binance
  asset". Ranking is deterministic and auditable, explicitly not AI and
  not a "biggest gainer" screen: every candidate is scored with the
  platform's own `strategy.generate_signal` (same EMA/ATR rules a
  manually-chosen pair gets), keeping only candidates with an
  actionable signal valid for the account's market type (long-only on
  Spot, long or short on Futures), ranked by EMA separation measured in
  units of that pair's own ATR. The scan only ever runs when
  `get_open_trade` finds nothing open for that account - enforced in
  `trader.py` itself, not only by `api/routers/account.py`'s open-trade
  guard on `PATCH /api/account` - so "never switch pairs while a
  position is open" holds in the backend trading logic regardless of
  what the API layer does or doesn't check.
- **Runtime status, persisted rather than assumed** (`trader.py`,
  `TradingAccount.last_scanned_at`/`last_signal`/`last_signal_at`/
  `last_error`/`last_error_at`): every scheduler cycle writes these,
  success or failure, so the dashboard reflects what actually happened
  rather than assuming the bot is working because nothing crashed.
  `last_error` is cleared on the next healthy cycle - it describes the
  most recent attempt, not a sticky fault flag - and is always a short
  category from `exchange.safe_error_message`, never a raw exception or
  traceback (those still go to the server log via `scheduler.py`'s
  `logger.exception`, untouched). `api/routers/account.py` derives a
  single `runtime_status` (`paused` > `not_started` > `error` >
  `trade_open` > `active`) from these plus `is_active`, the Start Bot
  gate, and an open-trade check, so the frontend never re-implements
  that priority logic itself. `paused` is only ever seen if something
  sets `is_active` directly (there is no customer control for it any
  more); `not_started` means every other live gate is met except the
  customer hasn't pressed Start Bot yet. Telegram only gets a new
  message when an error episode *starts* (`last_error` goes from `None`
  to set) and once, lifetime, the first time an account's mode resolves
  to LIVE (`live_notified_at`, which - since the Start Bot gate is the
  last condition checked - now doubles as the "bot started" message, no
  separate notification needed) - not every poll, which would just be
  noise. There is deliberately no "bot paused" notification, matching
  there being no customer-facing Pause.
- **Deploys separately from the trading engine's own host story:** the
  frontend is a static Vite build that goes to Vercel (or any static
  host) like any other SPA; the backend (API + scheduler, one process)
  still needs the persistent VPS described above - Vercel cannot run it.

## Exchange-side protective stops (live trades)

A live position is never protected only by software. The stop-loss is a
real order resting at Binance from the moment the trade is considered
open, so a dead VPS, scheduler, network or database cannot leave a real
position without a stop. **None of this has been validated against real
Binance yet** - see "Testnet validation procedure" below; passing the
mocked test-suite is not evidence about Binance.

### Modules

| Module | Responsibility |
|---|---|
| `venue.py` | Everything ccxt about *orders*: entry/exit/protective-stop placement, lookup by client id, cancel, exchange-truth reads (Futures position, Spot free base), market rules (lot step, tick, min qty/notional, allowed order types). Normalizes results. Spot and USD-M Futures subclasses. |
| `lifecycle.py` | The live-trade state machine: write-ahead entry, fill resolution, protect-then-open, emergency close, idempotent exit lease, reconciliation. The only place a live `Trade` changes state. |
| `exchange.py` | Symbol normalization (`BTC/USDT` -> ccxt `BTC/USDT:USDT` on the Futures client) and the Futures account-mode gate (One-way only; Hedge, Multi-Assets and Portfolio Margin refused). |
| `scheduler.py` | Startup reconciliation, the 5-minute poll, and the 15-second recovery loop for unhealthy trades, sharing one lock per account. |
| `instance_lock.py` | `flock` so a second scheduler process refuses to start. |

### Trade state machine

```
pending ──entry filled──▶ protecting ──stop verified──▶ open
   │                          │                           │ exit / stop fired
   │ no fill / old & absent   │ cannot protect            ▼
   ▼                          ▼                        closing ──▶ closed
 void                  emergency close ───────────────────▲
                              │ cannot verify flat
                              ▼
                          attention ──(reconciled)──▶ open | closed
```

`pending`/`protecting`/`open`/`closing`/`attention` are *non-terminal*;
`closed` (had exposure, now flat) and `void` (entry never happened) are
terminal. A partial unique index on `trades` allows at most one
non-terminal trade per account - the database-level form of the
one-position rule, and what stops a second entry while one is unresolved.

### Entry (write-ahead, idempotent)

1. `preflight_entry`: Futures contract must be flat (a pre-existing position
   is never stacked on nor covered by a `closePosition` stop); stale
   bot-prefixed open orders are cancelled.
2. A `pending` trade is **committed before any order is sent**.
3. The entry is a market order with a deterministic client id
   (`mba<24 hex of trade id>e`). On any error the bot looks the order up by
   that id; it is **never re-sent**. If Binance has no record yet the trade
   stays `pending` (blocking all other entries) and is voided only after
   `ENTRY_UNKNOWN_VOID_AFTER_SECONDS`; a Futures position with no order
   behind it goes to `attention`, never `void`.
4. The actual fill (average price, filled qty, base-asset commission) is
   persisted: `quantity` becomes what was really bought (Spot: net of the
   base commission, floored to the lot step).
5. The protective stop is placed with a deterministic id
   (`...s<attempt>`), read back from Binance, and only then does the trade
   become `open` and the customer is told. `protection_attempt` is
   committed before each placement, so a lost response is adopted, never
   duplicated.
6. If protection cannot be established (deterministic rejection, or the
   bounded retries run out) the bot closes the position immediately
   (`protection_failed_emergency_exit`). If that close cannot be verified
   the trade goes to `attention`, the customer is alerted once, and the
   recovery loop keeps working on it.

### Protective order mechanisms

*Futures (USD-M, One-way mode only)*: a `STOP_MARKET` with `closePosition`
(sized by Binance to the whole position at trigger time, so it can neither
increase nor reverse it), `workingType=MARK_PRICE`, tick-aligned so the
realized risk is never larger than planned. With ccxt 4.5.88 this is routed
through Binance's Algo Order endpoint; fetching, cancelling and listing it
**require `trigger=True`** (a reconciler that forgets it would see no stop).
If `closePosition` is rejected the fallback is `reduceOnly` with the
*exchange-confirmed* position size. A stop at or beyond the reported
liquidation price is refused. Every Futures exit is `reduceOnly`.

*Spot*: capability-aware. The order types and filters are read from the
loaded market of the actual symbol/environment: a market-triggered
`STOP_LOSS` is used only if the symbol lists it (no non-fill risk once
triggered, slippage accepted); otherwise `STOP_LOSS_LIMIT` (limit
`SPOT_STOP_LIMIT_BUFFER_PCT` below the trigger), whose gap-through/non-fill
risk is handled explicitly: once the price is at or below the limit with
the order still resting, the bot cancels it, confirms, and market-sells the
remainder (`stop_limit_gap`). If neither is listed, no entry is placed. OCO
is not used (the strategy has no take-profit leg).

**Spot protectable quantity (no fee is ever assumed).**
`protectable = floor_to_lot_step(min(bot_net_fill, free_base))`, where
`bot_net_fill = filled - base_commission` and the commission comes only from
exchange-confirmed data, in this order: (1) the entry order response (a
response listing fees in another asset, e.g. quote or BNB, confirms a base
commission of 0); (2) the account's own fill records for that order id
(`fetch_my_trades(orderId=...)`), accepted only if every fill carries a fee
and the fills account for the whole filled quantity. If neither establishes
it (after bounded retries) the bot does **not** size a stop: it takes the
emergency path (`quantity_unverifiable`) and sells at most the bot's own
gross fill, bounded by the free balance. The wallet may hold the customer's
own units of the same asset, so the free balance is only ever an upper
bound, re-read immediately before the stop is placed (and again if Binance
rejects it for insufficient funds). A pre-trade size filter keeps 0.2%
headroom for a possible base commission so that a planned entry is not too
small to protect; it is a filter, never a quantity. The stop must satisfy
`LOT_SIZE` and the minimum notional at the stop price.

**Stop-limit health watch.** A `STOP_LOSS_LIMIT` can be gapped through and
rest unfilled while looking protected, so these positions (only) get a fast
watch riding the 15 s recovery tick (`STOP_LIMIT_MONITOR_INTERVAL_SECONDS`,
default 15): one public ticker call per distinct symbol per pass, shared by
all customers, and nothing at all when no such position exists. Only a
position whose price has reached its trigger gets authenticated calls
(reconcile, then the gap check). If the price is at or below the executable
limit while the order still rests: claim the exit lease, cancel the stop,
confirm it terminal (a fill that won the race is recorded and no sell is
sent), re-read the remaining bot-attributable quantity (a partial fill
shrinks it), market-close only that remainder with the deterministic exit
id, record the actual fills, notify once. The 5-minute strategy scan and the
watch use the same lease, so they cannot both exit.

### Exit (one lease, one writer)

`lifecycle.execute_exit` is the only path that closes live exposure
(strategy exit, software backstop, emergency, resumed exits):

1. Atomic claim `open|protecting|attention -> closing` (compare-and-set).
2. Cancel the protective stop and read its final state. If it already
   executed, no exit order is sent for that part. Spot will not sell on top
   of a stop it cannot confirm cancelled.
3. Re-read exchange truth: Futures `|position|` (never more than the trade's
   quantity; opposite sign -> `attention`); Spot `min(quantity not yet sold,
   free base)`.
4. Exit order with a deterministic id (`...x<attempt>`), found-or-placed,
   Futures `reduceOnly`. Only the remainder is closed; residual exposure
   keeps the trade `closing` for the next pass.
5. P&L and exit price come from the exchange's own fills, not the stop price.

### Reconciliation

Runs at startup (every non-terminal live trade, SQLite is not trusted
alone), every poll for the account's trade, and every
`RECOVERY_INTERVAL_SECONDS` for trades that are not healthy-and-protected.
Per state: position+stop -> healthy; position without stop -> re-protect
(emergency close if it cannot); stop without position -> cancel the stale
order and close the record; stop fired while offline -> record it from the
fills, send no exit; closed outside the bot -> record, clean up; partial
stop execution -> close only the remainder; pending/unknown entry -> adopt
by client id or void/attention as above; unsupported account mode on an
existing position -> `attention`, no orders. Stale bot-prefixed orders are
cancelled; the customer's own orders (no `mba` client id prefix) are never
touched. Accounts in any non-terminal state cannot enter again.

### Withdrawal-permission policy

The runtime check (`exchange.has_withdrawal_permission`, an explicit
`canWithdraw: true`) is asked every live cycle but answered differently for
two distinct purposes, separated in code:

- **Entry-capable validation** (`exchange.assert_trade_only`, run inside
  `_maybe_enter` on every entry, independent of anything checked earlier):
  a key that can withdraw never reaches an order, Spot or Futures. The
  first detection sets `TradingAccount.withdrawal_blocked_at` (durable, so
  it survives restarts and the customer is alerted once). While it is set
  there are no entries, Start Bot is refused, and removing the permission
  on the same key does not lift it: only replacing the credential does
  (`PUT /api/account/credentials`), which stays impossible while any trade
  is non-terminal. The dashboard shows the block.
- **Risk-reducing management of an already-open position**: reconcile,
  verify or re-place the bot's own protective order, cancel only that order
  as part of the exit lease, the exit lease itself, emergency close and
  stop-trigger reconciliation all continue. They run on a `Venue` built with
  `entries_allowed=False` (the default), whose `place_entry` raises
  `EntryForbidden`; `open_live_trade` refuses such a venue too. Only the
  new-entry path builds an entry-capable venue, after `assert_trade_only`.
  Spot sells stay bounded by the bot's verified remaining quantity; Futures
  exits stay `reduceOnly`. A protected position is not liquidated merely
  because the permission exists. Once flat the account stays blocked.

### Secrets and logging

- `TelegramNotifier.send` never lets an httpx error escape: Telegram's Bot API
  puts the bot token in the request URL, so every failure is converted to a
  `NotificationError` carrying only a category and an HTTP status
  ("Telegram send failed: HTTP 403"). It is raised outside the `except`
  handlers, so it has no `__cause__`/`__context__` for a traceback printer to
  reach the original error.
- httpx logs every request URL at INFO (which would write the token on every
  successful send): httpx/httpcore are pinned to WARNING, and
  `log_safety.install_log_safety` adds a last-line redaction (configured
  secrets and anything shaped like a bot-URL token) to the process's log
  handlers (`main.py` and the API lifespan).
- `Settings` fields holding secrets are `repr=False`.
- Binance: ccxt exception text and tracebacks were verified (against a local
  server, with sentinel credentials) to contain no API key or secret; customer
  surfaces only ever get `exchange.safe_error_message`. See
  `tests/test_secret_logging.py`.

### What is deliberately not done

No customer control over the protective order, no Pause/Stop, no manual
trading controls. Account modes (Hedge, Multi-Assets, Portfolio Margin) are
refused rather than adapted to; nothing is ever switched on the customer's
account.

### Known residual risks

- Futures exit cancels the `closePosition` stop before the reduce-only exit
  (the specified order); for the milliseconds between them the position has
  no resting stop.
- Portfolio Margin has no dedicated "is this PM" read on the classic Futures
  API; it is refused because PM accounts are expected to fail the classic
  mode reads (to be confirmed by validation), plus a best-effort SAPI probe.
- Spot stop-limit gap-through is caught by the 15 s watch, not instantly; a
  market-triggered stop (when listed) has no such window. Each position in
  danger costs roughly 50 request-weight per pass (markets, account, order,
  open orders) until it is resolved.
- Binance behaviours the design relies on are listed in the next section.

### Testnet validation procedure

Run on the VPS (or any machine that reaches Binance) with operator-owned
*testnet* keys, **never** a customer account, and only then repeat the
read-only parts and a minimum-size canary on Mainnet. Record every result.
The automated tests use `tests/fake_binance.py`, whose behaviours below are
*assumptions*, and `tests/test_ccxt_contract.py`, which pins what is sent
through ccxt 4.5.88 (not what Binance accepts).

**Read-only (Testnet, then Mainnet)**
1. `exchangeInfo`: for BTC/ETH/BNB/XRP USDT on Spot, record `orderTypes`
   (is `STOP_LOSS` listed? `STOP_LOSS_LIMIT`?), `LOT_SIZE`, `MARKET_LOT_SIZE`,
   `PRICE_FILTER`, `NOTIONAL`/`MIN_NOTIONAL` (and whether it applies to
   market and stop orders). Same for the four USD-M perpetuals
   (`STOP_MARKET` listed?). Confirm `BTC/USDT:USDT` is the ccxt symbol and
   `load_markets` works with the production key.
2. Futures account reads with the production key: `fetch_position_mode`
   (`hedged` false), `GET multiAssetsMargin` (`false`), and what the Portfolio
   Margin probe returns on a classic account vs. a PM account if one is
   available. Confirm a PM account fails or is detected.
3. Confirm the key's permissions allow order placement but the withdrawal
   check (`canWithdraw`) behaves as `assert_trade_only` expects on Futures.

**Spot (Testnet)**
4. Market buy with `newClientOrderId`; confirm `fills[].commission/
   commissionAsset` and that the base commission is deducted from the held
   balance (net quantity matches `venue.net_quantity`).
5. Place the chosen protective stop (`STOP_LOSS` and, separately,
   `STOP_LOSS_LIMIT`) for the net quantity. Confirm: it rests (`NEW`), the
   base asset becomes `locked`, a sell exit is impossible until it is
   cancelled, a stop that would trigger immediately is rejected, and a
   duplicate `newClientOrderId` is rejected while it is open (and whether
   the id may be reused after cancel/fill).
6. Trigger the stop (move the market or use a stop near price): confirm fill
   price/fees; for `STOP_LOSS_LIMIT`, create a gap-through and confirm it
   stays open and unfilled.
7. Cancel-then-sell race: cancel while the price crosses; confirm the
   "unknown order"/filled cancel error and that `fetch_order` by client id
   gives the final state.
8. `-2013 Order does not exist` vs. lookups right after a timeout.

**Futures (Testnet; confirm the Algo Order path works there at all)**
9. Market entry, then `STOP_MARKET` + `closePosition=true` +
   `workingType=MARK_PRICE` through `/fapi/v1/algoOrder`. Confirm it rests,
   it is visible only with `trigger=True` (fetch, open orders, cancel by
   `clientAlgoId`), and what `algoStatus` transitions look like
   (`NEW`, `TRIGGERING`, `TRIGGERED`, `FINISHED`, `CANCELED`) and how ccxt
   maps them (the lifecycle treats ccxt "closed" as triggered).
10. After a trigger: position is flat, the conditional does not remain, and
    `fetch_my_trades` returns the fill (price/qty/side/time) used for P&L.
11. `reduceOnly` market exit on a flat position is rejected (-2022) and never
    opens a position; a duplicate `newClientOrderId` behaves as the fake
    assumes; `reduceOnly` + `closePosition` together are rejected.
12. A `closePosition` stop on a flat account, and on an account that was
    already flat when the stop would trigger: confirm it cannot open a
    position. Confirm what happens to a resting `closePosition` stop when the
    position is closed by other means (the bot cancels it either way).
13. Liquidation price field: how `liquidationPrice` is reported in cross vs
    isolated margin, since the "stop beyond liquidation" check depends on it.
14. Hedge Mode / Multi-Assets on a test account: confirm the gate refuses
    and no order is placed; flip mode mid-trade and confirm `attention`.

**Fault drills (Testnet, on the VPS)**
15. `kill -9` the service (a) right after an entry fills, (b) right after the
    stop is placed, (c) right after an exit fills. Restart; each must
    converge to a protected open trade or a closed trade with exactly one
    entry/stop/exit order.
16. Block outbound traffic to Binance for >5 minutes with a stop resting;
    let the price cross the stop; restore traffic. The position must have
    closed at Binance and the bot must record it without sending an exit.
17. Start two service instances; the second must exit immediately.
18. Revoke the key's trading permission with an open position: the bot must
    alert (error/attention) and send nothing.

**Mainnet canary (operator account, minimum size)** one Spot and one Futures
trade through entry, protection, a manual stop trigger and a strategy exit;
then the read-only checks again. No customer funds until all of the above
pass and are recorded.

## Data model

```
Customer 1---1 TradingAccount 1---1 ApiCredential
   |                  |
   1---N LegalAcceptance
                      1---N Trade
```

One customer, one trading account (see `models.py` for why this isn't a
harder constraint to relax later), one exchange credential, many trades. At most one trade per account may
be non-terminal (partial unique index; see "Exchange-side protective stops").
`Trade.is_paper` distinguishes simulated fills from real ones so a
customer's trial performance and live performance are never mixed in a
P&L query. `Customer` additionally carries `password_hash` and the
Paystack fields (`has_paid`, `paystack_reference`); neither is a
separate table since they're 1:1 with the customer, not something that
repeats. `TradingAccount.bot_code` (`botcode.py`, "MBA-7F3K9Q") is a
support-facing lookup handle, not a secret or an auth token - something a
customer can read aloud over WhatsApp so the operator can find their
account without needing their UUID.

## Module map

| Module | Responsibility |
|---|---|
| `indicators.py` | Pure EMA / ATR math. No I/O, no state. |
| `strategy.py` | EMA(20/50) crossover + ATR volatility filter -> a `StrategySignal`. Stateless: doesn't know about open positions. |
| `risk.py` | Position sizing (risk % of balance / stop distance) and stop-loss placement. Enforces the 1-2% cap. |
| `onboarding.py` | Trial start/expiry, the live-trading bypass and its logged disclosure/timestamp. |
| `legal.py` | Terms/Privacy/Risk-Disclosure document versions and the single "is this customer's acceptance current" check. |
| `crypto_utils.py` | Fernet encryption for stored API credentials. |
| `botcode.py` | Generates the support-facing "MBA-XXXXXX" account code. |
| `models.py` / `db.py` | SQLAlchemy schema and session/query helpers. |
| `exchange.py` | ccxt client construction (spot vs futures, sandbox vs live) and the trade-only permission check. |
| `notifications/` | `Notifier` interface; `TelegramNotifier` implemented, `WhatsAppNotifier` a stub per the "added later" spec. |
| `trader.py` | One account's evaluation cycle: fetch data -> signal -> enter/exit/manage position -> notify. Paper trades are simulated here; live trades are opened and managed through `lifecycle.py`. |
| `venue.py` / `lifecycle.py` / `instance_lock.py` | Exchange order layer, live-trade state machine, single-instance lock. See "Exchange-side protective stops". |
| `scheduler.py` | The shared loop described above, plus startup reconciliation and the fast recovery loop. |
| `api/app.py` | FastAPI app: CORS, router wiring, and the lifespan hook that starts `scheduler.run_forever` as a background task. |
| `api/security.py` | Password hashing (bcrypt) and session JWT create/verify. |
| `api/routers/auth.py` | Signup (requires Terms/Privacy consent) /login/logout/accept-legal; sets/clears the session cookie. |
| `api/routers/account.py` | Onboarding (create account + credential), settings updates (pair/market/risk/trading-capital/pair-selection-mode), trial-bypass, credential updates, the separate enable-live-trading step (requires risk-disclosure consent), and the final explicit start-bot step (requires trading capital configured) - as HTTP, all gated by `require_legal_acceptance`. No customer-facing pause/resume any more. |
| `api/routers/telegram.py` | Deep-link generation + webhook that resolves a link token to a chat id. |
| `api/routers/billing.py` | Paystack one-time payment initialization and webhook. |
| `frontend/` | React + Vite + Tailwind SPA: landing, signup/login, onboarding wizard, dashboard. Deploys to Vercel. |
| `alembic/` | Schema migrations (`alembic revision --autogenerate`, `alembic upgrade head`) - the authority for a deployed database's schema; `db.init_db`'s `create_all` is dev/test convenience only. See README's "Database migrations". |

## Not built yet (out of scope for this pass)

- Backtesting harness for the strategy parameters.
- WhatsApp notifications (interface exists, implementation doesn't).
- Automated UI tests for `frontend/` (manual walkthrough only, matching
  the backend's own testing approach before this pass).
