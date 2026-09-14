# MBA Bot

A multi-tenant crypto trading bot: one backend, many customers, each
identified by their own encrypted Binance API key. Nobody touches a
server or writes config - a customer picks a pair, spot or futures, and a
notification channel, and the bot trades their account from there.

**This is not financial advice and carries real financial risk once an
account leaves paper mode.** See "Paper trading and the live bypass"
below before pointing this at real funds.

## What it does

- **Strategy:** EMA(20/50) crossover trend-following, with an ATR
  volatility filter that skips signals when the market is flat/range-bound
  (crossover strategies lose money chopping sideways).
- **Risk:** every trade risks 1-2% of account balance (customer-set,
  hard-capped), sized off an ATR-based stop-loss distance - never a fixed
  coin quantity.
- **Onboarding:** every account starts in a 7-day paper trial on Binance
  testnet with simulated funds. Skipping straight to live trading requires
  checking a disclosure box, logged with a timestamp against the account.
- **Notifications:** Telegram messages on entries, exits, and daily P&L
  summaries. WhatsApp is planned but not implemented (see
  `src/mba_bot/notifications/whatsapp.py`).
- **Security:** exchange API keys are provisioned trade-only (no
  withdrawal) on the exchange side, encrypted at rest with Fernet, and
  spot-checked at runtime before live trading.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the deployment-shape
recommendation (one shared process, not one process per customer), the
paper-trading design, and the module map.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # includes pytest; use requirements.txt for prod-only

cp .env.example .env
python -m mba_bot.crypto_utils        # prints a Fernet key -> paste into .env as ENCRYPTION_KEY
```

Edit `.env`:

- `TESTNET_API_KEY` / `TESTNET_API_SECRET` - one Binance testnet key
  ([testnet.binance.vision](https://testnet.binance.vision)), shared by
  every account's paper trial.
- `TELEGRAM_BOT_TOKEN` - from [@BotFather](https://t.me/BotFather), needed
  before any account using `notification_channel=telegram` can receive
  messages.
- Everything else has a sane default (see `.env.example`).

Note the package lives under `src/`; set `PYTHONPATH=src` (or install the
project - see `pyproject.toml`) if you're not running via `pytest`/the
provided scripts, both of which already handle it.

## Signing up a customer

There's no signup UI yet, so `scripts/signup_customer.py` is the stand-in
(a real signup form should call the same `mba_bot.onboarding` functions,
not reimplement the trial/bypass logic):

```bash
python scripts/signup_customer.py \
  --email customer@example.com \
  --pair BTC/USDT --market-type spot \
  --notification-channel telegram --notification-target <telegram-chat-id> \
  --api-key <customer-mainnet-key> --api-secret <customer-mainnet-secret>
```

This creates the account in the 7-day paper trial. To skip straight to
live trading (only after the customer has actually seen and agreed to the
disclosure text in `onboarding.LIVE_TRADING_DISCLOSURE`):

```bash
python scripts/signup_customer.py ... --skip-trial --accept-live-risk-disclosure
```

## Running the bot

```bash
python -m mba_bot.main
```

This runs the shared scheduler loop: every `POLL_INTERVAL_SECONDS`, it
evaluates every active account concurrently (paper accounts against
Binance testnet, live accounts against mainnet with the customer's own
key) and places/manages trades.

## Paper trading and the live bypass

Every new account trades on Binance testnet with a simulated balance
(`paper_balance`, tracked per-account in the database - see
`ARCHITECTURE.md` for why this isn't a real testnet balance) for 7 days by
default. An account moves to live trading either when the trial expires
naturally, or when the customer explicitly checks the bypass box; either
way, `onboarding.resolve_trading_mode` is the single place that decides
which mode an account is in, and it fails closed - any ambiguity resolves
to paper, never live.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests cover the pure logic - indicators, strategy signal generation, risk
sizing, encryption, and trial/bypass resolution - without needing network
access or exchange credentials. `exchange.py`, `trader.py`, and
`scheduler.py` aren't unit tested here since they're thin glue over ccxt
and the DB; exercise them against Binance testnet before trusting them
with a live account.
