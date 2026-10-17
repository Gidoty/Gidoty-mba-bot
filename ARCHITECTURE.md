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

So: the platform holds **one** Binance testnet key
(`TESTNET_API_KEY`/`TESTNET_API_SECRET`), used for every account's 7-day
trial. What's customer-specific isn't the exchange credential, it's the
simulated ledger - `TradingAccount.paper_balance` tracks each account's
own equity curve independently in our database, starting from
`PAPER_STARTING_BALANCE`. Orders during the trial aren't actually sent to
the exchange at all (see `trader._maybe_enter`/`_maybe_exit`: `is_paper`
skips `client.create_market_order` and only writes a `Trade` row) - only
market data (OHLCV) is fetched from testnet, and P&L is computed locally
against `paper_balance`. This keeps paper accounts free of any real
funds or exchange-side state, and makes "how much would I have made"
purely a property of our own records.

The customer's own mainnet key (`ApiCredential`, encrypted at rest) is
collected at signup but stays unused until the account reaches live mode
(trial expiry or the logged bypass, and the customer has paid) - see
`onboarding.py`.

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
- **Billing and the lock, by design** (`api/routers/billing.py`,
  `onboarding.resolve_trading_mode`): the free trial is unlimited, the
  payment wall is in front of LIVE only. A new account's
  `TradingAccount.is_active` defaults `True` (that field is the
  customer's own pause/resume toggle, nothing more) and
  `db.list_active_accounts` - the only thing the scheduler calls to
  decide what to run this tick - only checks that. Payment is enforced
  one layer in, by `resolve_trading_mode`: `has_paid=False` pins an
  account to PAPER forever regardless of the trial clock or a logged
  bypass, and PAPER never places a real order (see "Paper trading"
  above), so an unpaid account can run indefinitely at zero financial
  risk - it just can never reach LIVE. This keeps three concerns
  independent: a customer can pause/resume their own account via
  `is_active`, test the bot for as long as they like for free, but can't
  reach live trading by touching anything client-side (the `PATCH
  /api/account` endpoint never sets `has_paid`, and `has_paid` is
  checked in exactly one place - `resolve_trading_mode` - so the
  scheduler's own account filter and the mode decision can't drift out
  of sync with each other).

  `has_paid` flips exactly two ways, both ending in the same place:
  - Paystack's hosted payment page (card data never touches this server)
    for a one-time charge, confirmed by a webhook that verifies
    Paystack's own signature (HMAC-SHA512 over the raw body, keyed by the
    same secret key used for API calls - no separate webhook secret like
    Stripe has). `apply_payment_event` is split from that
    signature-verification code so it's unit-testable with a hand-built
    event dict - see `tests/test_api_billing.py`.
  - `scripts/mark_customer_paid.py`, run by the operator over SSH, for
    anyone who paid outside Paystack (bank transfer, cash, a comp). Same
    flag, same effect - the scheduler can't tell the difference and isn't
    meant to.

  Lifetime access, not a subscription: once `has_paid` is set it never
  gets cleared by anything in this codebase.
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
  from the old pair has nothing to say about the new one.
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
  single `runtime_status` (`paused` > `error` > `trade_open` > `active`)
  from these plus `is_active` and an open-trade check, so the frontend
  never re-implements that priority logic itself. Telegram only gets a
  new message when an error episode *starts* (`last_error` goes from
  `None` to set) and once, lifetime, the first time an account's mode
  resolves to LIVE (`live_notified_at`) - not every poll, which would
  just be noise.
- **Deploys separately from the trading engine's own host story:** the
  frontend is a static Vite build that goes to Vercel (or any static
  host) like any other SPA; the backend (API + scheduler, one process)
  still needs the persistent VPS described above - Vercel cannot run it.

## Data model

```
Customer 1---1 TradingAccount 1---1 ApiCredential
                      |
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
| `crypto_utils.py` | Fernet encryption for stored API credentials. |
| `botcode.py` | Generates the support-facing "MBA-XXXXXX" account code. |
| `models.py` / `db.py` | SQLAlchemy schema and session/query helpers. |
| `exchange.py` | ccxt client construction (spot vs futures, sandbox vs live) and the trade-only permission check. |
| `notifications/` | `Notifier` interface; `TelegramNotifier` implemented, `WhatsAppNotifier` a stub per the "added later" spec. |
| `trader.py` | One account's evaluation cycle: fetch data -> signal -> enter/exit/manage position -> notify. |
| `scheduler.py` | The shared loop described above. |
| `api/app.py` | FastAPI app: CORS, router wiring, and the lifespan hook that starts `scheduler.run_forever` as a background task. |
| `api/security.py` | Password hashing (bcrypt) and session JWT create/verify. |
| `api/routers/auth.py` | Signup/login/logout; sets/clears the session cookie. |
| `api/routers/account.py` | Onboarding (create account + credential), settings updates, pause/resume, live-bypass - as HTTP. |
| `api/routers/telegram.py` | Deep-link generation + webhook that resolves a link token to a chat id. |
| `api/routers/billing.py` | Paystack one-time payment initialization and webhook. |
| `frontend/` | React + Vite + Tailwind SPA: landing, signup/login, onboarding wizard, dashboard. Deploys to Vercel. |
| `alembic/` | Schema migrations (`alembic revision --autogenerate`, `alembic upgrade head`) - the authority for a deployed database's schema; `db.init_db`'s `create_all` is dev/test convenience only. See README's "Database migrations". |

## Not built yet (out of scope for this pass)

- Backtesting harness for the strategy parameters.
- WhatsApp notifications (interface exists, implementation doesn't).
- Automated UI tests for `frontend/` (manual walkthrough only, matching
  the backend's own testing approach before this pass).
