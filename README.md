# MBA Bot

A multi-tenant crypto trading bot sold as a self-serve product: a
customer signs up on the website, picks a pair, spot or futures, links
Telegram, and hands over their own encrypted Binance API key - no manual
step from the operator, no server access, no config file.

**This is not financial advice and carries real financial risk once an
account leaves paper mode.** See "Paper trading and the live bypass"
below before pointing this at real funds. Running a paid service that
trades other people's money is also regulated in most jurisdictions -
get real legal advice before taking on paying customers, independent of
how well the code is written.

## What it does

- **Self-serve signup:** a React frontend (`frontend/`) talking to a
  FastAPI backend (`src/mba_bot/api/`) - email/password auth, an
  onboarding wizard (pair, market type, risk %, Binance key), a Telegram
  linking flow, a dashboard (trial/live status, trade history, pause
  toggle), and Stripe billing.
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
  summaries. An account can't trade until Telegram is linked. WhatsApp is
  planned but not implemented (see `src/mba_bot/notifications/whatsapp.py`).
- **Billing:** Stripe Checkout + a webhook that pauses the trading
  account automatically if a subscription lapses.
- **Security:** exchange API keys are provisioned trade-only (no
  withdrawal) on the exchange side, encrypted at rest with Fernet, and
  spot-checked at runtime before live trading.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the deployment-shape
recommendation (one shared process, not one process per customer), the
paper-trading design, and the module map.

## Deployment shape

Two independent deploys, same split as a typical SPA + API product:

- **Backend** (`src/mba_bot/api/` + the trading scheduler) needs a
  persistent server - a VPS (Hetzner/DigitalOcean/etc.), not a serverless
  platform. One process serves the HTTP API *and* runs the trading loop
  as a background task (see `api/app.py`'s lifespan hook) - there's only
  ever one thing to deploy and supervise.
- **Frontend** (`frontend/`) is a static Vite build and deploys to
  **Vercel** (or any static host) - set `VITE_API_URL` to the backend's
  public URL + `/api`.

Vercel cannot run the backend - it has no persistent process, and this
bot needs one running continuously.

## Backend setup

```bash
cd mba-bot  # repo root
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # includes pytest; use requirements.txt for prod-only

cp .env.example .env
python -m mba_bot.crypto_utils        # prints a Fernet key -> paste into .env as ENCRYPTION_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"  # paste into .env as JWT_SECRET
```

Fill in `.env` (see `.env.example` for the full list and comments):

- `TESTNET_API_KEY` / `TESTNET_API_SECRET` - one Binance testnet key
  ([testnet.binance.vision](https://testnet.binance.vision)), shared by
  every account's paper trial.
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_USERNAME` - create a bot via
  [@BotFather](https://t.me/BotFather); needed before any account can
  link Telegram (and therefore before it can trade at all).
- `FRONTEND_ORIGIN` - the frontend's URL; CORS only allows this origin.
- `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` / `STRIPE_PRICE_ID` - from
  your Stripe dashboard, only needed once you're ready to charge for
  access.
- Everything else has a sane default.

Run the API (and the trading scheduler, in the same process):

```bash
uvicorn mba_bot.api.app:app --app-dir src --reload --port 8000
```

Note the package lives under `src/`; `--app-dir src` (or `PYTHONPATH=src`)
is how `uvicorn`/scripts find it - `pytest` and the provided scripts
already handle this themselves.

## Frontend setup

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /api -> localhost:8000
```

`npm run build` produces a static `dist/` to deploy to Vercel. Set
`VITE_API_URL` (see `frontend/.env.example`) for a production build
pointed at a deployed backend; leave it unset for local dev.

## Admin/manual signup (no website needed)

`scripts/signup_customer.py` creates a customer + account directly in the
database - useful for testing, or for onboarding a customer by hand
without the website. It calls the same `mba_bot.onboarding` functions the
API does, not a parallel implementation:

```bash
python scripts/signup_customer.py \
  --email customer@example.com --password "a-strong-password" \
  --pair BTC/USDT --market-type spot \
  --notification-channel telegram --notification-target <telegram-chat-id> \
  --api-key <customer-mainnet-key> --api-secret <customer-mainnet-secret>
```

(`--notification-target` is the numeric Telegram chat id directly, since
there's no web flow here to walk through the `/api/telegram/link-url` +
webhook dance - get it by having the customer message the bot once and
reading the chat id off `getUpdates`, or just use the website instead.)

To skip straight to live trading (only after the customer has actually
seen and agreed to the disclosure text in
`onboarding.LIVE_TRADING_DISCLOSURE`):

```bash
python scripts/signup_customer.py ... --skip-trial --accept-live-risk-disclosure
```

## Paper trading and the live bypass

Every new account trades on Binance testnet with a simulated balance
(`paper_balance`, tracked per-account in the database - see
`ARCHITECTURE.md` for why this isn't a real testnet balance) for 7 days by
default. An account moves to live trading either when the trial expires
naturally, or when the customer explicitly accepts the bypass (a checkbox
on the dashboard, or `--skip-trial` on the CLI); either way,
`onboarding.resolve_trading_mode` is the single place that decides which
mode an account is in, and it fails closed - any ambiguity resolves to
paper, never live. `trader.py` additionally refuses to trade any account
until its notification channel is actually linked.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Covers the pure logic (indicators, strategy signal generation, risk
sizing, encryption, trial/bypass resolution), the auth/billing helper
modules (password hashing, JWT round-trips, Stripe webhook event
handling), and an end-to-end smoke test of the HTTP API using FastAPI's
in-process `TestClient` (signup -> onboarding -> Telegram link -> live
bypass) - all without network access or real exchange/Stripe/Telegram
calls. `exchange.py` and the live parts of `trader.py`/`scheduler.py`
aren't exercised by the test suite since they're thin glue over ccxt;
exercise them against Binance testnet before trusting them with a live
account.
