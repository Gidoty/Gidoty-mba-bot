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
  toggle), and Paystack billing.
- **Strategy:** EMA(20/50) crossover trend-following, with an ATR
  volatility filter that skips signals when the market is flat/range-bound
  (crossover strategies lose money chopping sideways).
- **Risk:** every trade risks 1-2% of account balance (customer-set,
  hard-capped), sized off an ATR-based stop-loss distance - never a fixed
  coin quantity.
- **Onboarding:** every account starts in a 7-day paper trial on Binance
  testnet with simulated funds. Skipping straight to live trading requires
  checking a disclosure box, logged with a timestamp against the account.
- **Locked by design, not just by payment:** there's no free trial - a
  brand-new account doesn't trade (paper or live) until the operator
  unlocks it. That happens automatically on a Paystack payment, or
  manually at any time with `scripts/mark_customer_paid.py` for a
  customer who paid some other way (bank transfer, cash, a comp) - both
  just flip the same `has_paid` flag.
- **Notifications:** Telegram messages on entries, exits, and daily P&L
  summaries. An account can't trade until Telegram is linked. WhatsApp is
  planned but not implemented (see `src/mba_bot/notifications/whatsapp.py`).
- **Billing:** a one-time Paystack payment unlocks lifetime access - no
  subscription, nothing to renew or cancel.
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
- `PAYSTACK_SECRET_KEY` / `PAYSTACK_AMOUNT` / `PAYSTACK_CURRENCY` - from
  your Paystack dashboard, only needed once you're ready to charge for
  access. One secret key both signs API calls and verifies webhook
  signatures - Paystack has no separate webhook secret.
- Everything else has a sane default.

Create the database schema (see "Database migrations" below for what
this actually does):

```bash
alembic upgrade head
```

Run the API (and the trading scheduler, in the same process):

```bash
uvicorn mba_bot.api.app:app --app-dir src --reload --port 8000
```

Note the package lives under `src/`; `--app-dir src` (or `PYTHONPATH=src`)
is how `uvicorn`/scripts find it - `pytest` and the provided scripts
already handle this themselves.

## Database migrations

Schema changes go through [Alembic](https://alembic.sqlalchemy.org/),
not `db.init_db`'s `create_all` (which only ever creates missing
*tables* - it silently does nothing to a table that already exists but
is missing a new column, which is exactly what broke in production
once before this was wired in).

**After changing a model in `models.py`:**

```bash
alembic revision --autogenerate -m "describe the change"
```

Open the generated file in `alembic/versions/` and read it before
trusting it - autogenerate is a good first draft, not a guarantee (it
can miss things like renamed columns, which it sees as a drop + an add).
Commit the migration file alongside the model change, same PR/commit.

**Deploying that change** (local dev or the VPS alike):

```bash
git pull
pip install -r requirements.txt   # alembic itself, or any new dependency
alembic upgrade head
sudo systemctl restart mba-bot    # VPS only
```

**One-time note for this project's VPS specifically:** its database was
created by `create_all` before Alembic existed in this repo, then had
`has_paid`/`paystack_reference` added by hand (see git history for that
incident). Alembic doesn't know about either of those unless told, so
before the *first* `alembic upgrade head` on that database, run this
once instead:

```bash
alembic stamp head
```

`stamp` records "this database is already at this revision" without
running any DDL - exactly right for a database that's already correct
but was never under Alembic's tracking. Running `upgrade head` instead
at that point would try to `CREATE TABLE` on tables that already exist
and fail. Any database created via `alembic upgrade head` from the
start (a fresh dev setup, a fresh install) never needs this - `stamp`
is a one-time fix for this specific VPS's history, not a normal step.

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

## Unlocking an account

Every account - however it was created - starts **locked**: the scheduler
won't trade it, paper or live, until `Customer.has_paid` is set. That
happens automatically when the Paystack webhook sees a real payment, or
you can unlock it yourself for anyone who paid some other way:

```bash
python scripts/mark_customer_paid.py --email customer@example.com
python scripts/mark_customer_paid.py --email customer@example.com --reference "UBA transfer 12 Oct"
```

`--reference` is just a free-text note for your own records (e.g. a bank
transfer reference) - it's stored but nothing reads it back. Running it
again on an already-unlocked customer is a no-op, not an error. You can
also unlock immediately at signup time with `signup_customer.py
--mark-paid`.

Run this on the server itself (SSH) - it has no authentication of its
own beyond whoever can run commands on your VPS, which is the point:
only you can unlock an account, there's no code the customer enters.

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
modules (password hashing, JWT round-trips, Paystack webhook event
handling), and an end-to-end smoke test of the HTTP API using FastAPI's
in-process `TestClient` (signup -> onboarding -> Telegram link -> live
bypass) - all without network access or real exchange/Paystack/Telegram
calls. `exchange.py` and the live parts of `trader.py`/`scheduler.py`
aren't exercised by the test suite since they're thin glue over ccxt;
exercise them against Binance testnet before trusting them with a live
account.
