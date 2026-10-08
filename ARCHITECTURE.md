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
(trial expiry or the logged bypass) - see `onboarding.py`.

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
  `SameSite=Lax` cookie (`api/security.py`) - no token for the frontend
  to manage itself, no OAuth integration for v1.
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
- **Billing** (`api/routers/billing.py`) uses Stripe Checkout (hosted
  page - card data never touches this server) and a webhook that maps
  Stripe's own subscription status onto `Customer.subscription_status`
  and `TradingAccount.is_active`. The event-handling logic
  (`apply_subscription_event`) is split from the signature-verification
  code around it specifically so it can be unit tested with a
  hand-built event dict - see `tests/test_api_billing.py`.
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
Stripe fields (`stripe_customer_id`, `subscription_status`); neither is
a separate table since they're 1:1 with the customer, not something that
repeats.

## Module map

| Module | Responsibility |
|---|---|
| `indicators.py` | Pure EMA / ATR math. No I/O, no state. |
| `strategy.py` | EMA(20/50) crossover + ATR volatility filter -> a `StrategySignal`. Stateless: doesn't know about open positions. |
| `risk.py` | Position sizing (risk % of balance / stop distance) and stop-loss placement. Enforces the 1-2% cap. |
| `onboarding.py` | Trial start/expiry, the live-trading bypass and its logged disclosure/timestamp. |
| `crypto_utils.py` | Fernet encryption for stored API credentials. |
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
| `api/routers/billing.py` | Stripe Checkout session creation, webhook, billing portal link. |
| `frontend/` | React + Vite + Tailwind SPA: landing, signup/login, onboarding wizard, dashboard. Deploys to Vercel. |

## Not built yet (out of scope for this pass)

- DB migrations (Alembic) - `db.init_db` just calls `create_all`, fine
  until the schema needs to change under live data.
- Backtesting harness for the strategy parameters.
- WhatsApp notifications (interface exists, implementation doesn't).
- Automated UI tests for `frontend/` (manual walkthrough only, matching
  the backend's own testing approach before this pass).
