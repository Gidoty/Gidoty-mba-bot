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

## Data model

```
Customer 1---1 TradingAccount 1---1 ApiCredential
   |                  |
   1---N LegalAcceptance
                      1---N Trade
```

One customer, one trading account (see `models.py` for why this isn't a
harder constraint to relax later), one exchange credential, many trades.
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
| `trader.py` | One account's evaluation cycle: fetch data -> signal -> enter/exit/manage position -> notify. |
| `scheduler.py` | The shared loop described above. |
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
