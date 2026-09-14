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
P&L query.

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

## Not built yet (out of scope for this pass)

- A signup web UI / API - `scripts/signup_customer.py` is the stand-in,
  calling the same `onboarding`/`models` functions a real signup endpoint
  should call.
- DB migrations (Alembic) - `db.init_db` just calls `create_all`, fine
  until the schema needs to change under live data.
- Backtesting harness for the strategy parameters.
- WhatsApp notifications (interface exists, implementation doesn't).
