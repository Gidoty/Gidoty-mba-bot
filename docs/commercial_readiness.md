# Commercial readiness: switches, legal texts, consent, data requests

This page lists what an operator sets to take the service from "closed beta" to "open to customers", what each customer sees, and how the legal texts, consent and data requests behave. It applies to every account the same way: no switch, default or branch is tied to one customer. Nothing here is run by the code on its own; the operator sets each value in the server environment and restarts.

## 1. The switches

"Closed beta" means a small group the operator invited, with registration shut to strangers. "Open" means anyone may register and subscribe. Values not listed keep their default.

| Variable | Default | Closed beta | Open | What a customer sees |
|---|---|---|---|---|
| `SIGNUPS_MODE` | `open` | `closed` | `open` | Closed: the sign-up page says registration is closed and shows the contact e-mail; a direct signup request is refused (HTTP 403) with the same message and no account is created. Existing customers log in and use the product as before. Open: the normal sign-up form. Any value other than `open` counts as closed, so a typo never leaves registration open. |
| `SUBSCRIPTION_BILLING_ENABLED` | off | on once Paystack is configured and tested | on | Off: payment works as the older one-time activation. On: an Activate screen with the monthly price in naira (card) and, if enabled, dollars (USDT); the state of the subscription (active, grace, cancelled); reminders. A registered but unpaid customer sees Activate in either mode. |
| `PAYSTACK_RENEWAL_RECONCILE` | off | off until the operator has watched one real renewal | on, by the operator by hand (it uses the Paystack secret key) | Nothing visible. An hourly read-only check that applies a renewal whose webhook never settled, only from a verified successful payment. |
| `NOWPAYMENTS_ENABLED` (+ `NOWPAYMENTS_IPN_MODE`, `NOWPAYMENTS_PAY_CURRENCY`, `NOWPAYMENTS_IPN_URL`, `NOWPAYMENTS_PRICE_USD`; the key and the IPN secret are typed into the server environment by the operator and never shared) | off | optional | optional, after one observed real payment (see `docs/security/billing_nowpayments.md`) | On: a second "Pay with USDT" button. Off: card only. |
| `ADMIN_DASHBOARD_ENABLED` (+ `ADMIN_EMAILS`) | off | on | on | Nothing: it is for the operator only. Customers never see it and cannot reach it. |
| `ACCOUNT_DAILY_LOSS_LIMIT` | off | **on** | **on** | The Risk tab asks for a daily loss limit in dollars and live trading needs one. The legal texts describe this limit, so it must be on before customers trade live. |
| `ACCOUNT_TOTAL_LOSS_STOP` | off | **on** | **on** | The fixed 50% total loss stop and its reset button. Described in the legal texts: on before customers trade live. |
| `PROTECTION_PRECHECK_MODE` | `observe` | `enforce` | `enforce` | Futures entries are refused when the stop-loss would sit too close to the estimated liquidation level. Described in the legal texts. |
| `EMA_CLOSED_CANDLE_ONLY` (+ `EMA_ENTRY_WINDOW_MINUTES`, default 10) | off | on | on | Nothing visible. The default strategy decides on closed candles only and ignores a signal more than the window after the candle closed. |
| `LIVE_RSI_MR`, `LIVE_DONCHIAN` (with `RSI_MR_ENABLED`, `DONCHIAN_ENABLED`, `NEW_STRATEGY_GUARDS_MODE`) | off | off | off unless the operator chooses otherwise | Nothing. These two strategies run only on accounts the operator opts in directly in the database (a customer cannot opt in), and only when both the strategy and its live switch are on. With them off every account runs the default strategy only. The legal texts already say that extra strategies are newer and less proven. |
| `LIVE_MULTI_POSITION` | off | off | on only when the operator wants customers to hold up to 3 trades | On: Bot Settings offers 1 to 3 trade slots and extra pairs (Futures). Off: one live trade at a time. |
| `REPORTING_V2`, `SETUP_ADVISOR`, `SMALL_CAPITAL_PAIRS` | see `.env.example` | on | on | Results by mode and strategy with "before fees" labels; setting notes with real numbers; skipped small pairs explained. The legal texts describe the fee labels, so keep `REPORTING_V2` on. |
| `RISK_PCT_MAX` | 2 | 2 | 2, or higher only deliberately (hard ceiling 40) | The most a customer may choose for risk per trade. Above 2% and from 10% the customer must confirm; each confirmation is recorded. |

Also required for open sales (names only, values are the operator's): `JWT_SECRET`, `ENCRYPTION_KEY`, `COOKIE_SECURE` on, `LOGIN_THROTTLE_ENABLED` and `RATE_LIMIT_ENABLED` on, `DISABLE_API_DOCS` on, the Paystack variables listed in `docs/security/billing_paystack_subscriptions.md`, the kill switch file (`docs/safety/kill_switch.md`), and a backup routine for the database. `TRADE_TELEMETRY` (research logging) is optional.

### Using SIGNUPS_MODE to test billing on the live system
1. Set `SIGNUPS_MODE=closed` and restart: strangers cannot register.
2. Registered customers (the operator's own test customer included) can still log in, see Activate and pay.
3. Set `SIGNUPS_MODE=open` and restart when ready. There is no other effect: login, billing, existing accounts and the admin dashboard do not read the setting.

## 2. Legal texts and consent

The Terms of Service, the Trading Risk Disclosure and the Privacy Policy live in `frontend/src/legalContent.js` and are served at `#/terms`, `#/risk-disclosure` and `#/privacy`. Every statement describes what the code does. The Terms contain no statement about whether the operator is licensed, registered, approved, exempt or regulated, and none about the opposite; there is a marker comment `REGULATORY-STATUS-SLOT` after section 1 of the Terms where the operator can add one sentence. A test fails if a regulatory-status word appears in the texts, so adding the sentence means updating that test on purpose.

Versions: each document's version is its effective date (`TERMS_VERSION`, `PRIVACY_VERSION`, `RISK_DISCLOSURE_VERSION` in `src/mba_bot/legal.py`, repeated in `legalContent.js`; a test checks they agree). Changing a text means changing both versions.

| Event | What the customer sees | What is recorded |
|---|---|---|
| Sign-up | Two boxes: Terms, Privacy | one row each, with version and time |
| A version of Terms or Privacy changes | After login a consent screen (two boxes) before the dashboard; the account API answers 403 `ACCEPTANCE_REQUIRED` until accepted | new rows; old rows stay as history |
| First live activation | The Risk Disclosure box in the "Enable live trading" confirmation. The screen sends the version it showed; the server refuses (409) if that is not the current version | a `risk_disclosure` row with version and time |
| The Risk Disclosure version changes | Only customers who already enabled live trading are asked again: the consent screen shows a third box. A customer who never went live is asked at activation | a new `risk_disclosure` row |
| Risk setting above 2% or from 10% | The existing confirmation (unchanged) | `risk_ack_high` / `risk_ack_very_high` rows |

### Consent never touches the trading engine
Consent gates the customer's API and screens only. The scheduler, trader, lifecycle, reconciler, protective stops and exits never read it: a customer with an open live trade who has not accepted a new version keeps that trade managed (stop-loss, exits, reconciliation, notifications) until it closes. `tests/test_commercial_consent.py` proves it with a live Futures trade under three consent states (none, old version, current) and with a scan that fails if any engine module imports the legal module. What it does not do: it does not stop NEW entries for a customer who has not accepted; the bot keeps opening trades per the customer's own settings. Whether to stop new entries until re-acceptance is a product decision (see section 5).

## 3. Data requests

A logged-in customer can ask for a copy of their data or for deletion: Account page, "Your data". The request is recorded in `data_requests` (one open request per customer and kind). It needs a login but not the current Terms acceptance. Nothing is exported or deleted by this route and nothing in the service deletes anything on a timer.

Operator side (read only; run on the server with the app's `DATABASE_URL`):

    cd /opt/mba-bot && PYTHONPATH=src .venv/bin/python -m mba_bot.data_requests list
    cd /opt/mba-bot && PYTHONPATH=src .venv/bin/python -m mba_bot.data_requests export --customer <first 8+ characters> --out export.json

The export holds e-mail, sign-up date, account settings, trades, acceptances, subscription and payment summaries and the customer's data requests. It never holds the password hash, the encrypted exchange keys, the Telegram chat id, provider references or tokens. The operator sends it to the e-mail on the account.

Deletion is manual and is the operator's decision. There is no deletion tool in this release (see section 5). Records kept after an approved deletion, and why, are stated in the Privacy Policy and in the reply the customer gets: payment and subscription records (reconciliation, refunds, disputes, accounting); the customer's acceptances with version and time (proof of what was agreed); the record of the request and its outcome. Before deleting, the customer closes open trades and revokes the Binance key themselves: deleting data closes nothing on Binance.

Retention, as the code works: no automatic deletion exists. Data stays until the customer asks and the operator deletes it, or the operator removes it. The Privacy Policy says exactly this.

## 4. What each customer sees, by stage

| Stage | Registration | Activate screen | Live trading |
|---|---|---|---|
| Closed beta | closed to strangers (`SIGNUPS_MODE=closed`) | monthly subscription (card; USDT if enabled) | after payment, practice period (or chosen skip), a trade-only Binance key, a daily loss limit, the Risk Disclosure box and Start Bot |
| Open | open | same | same |

## 5. Not built in this release (decisions or follow-ups)
* A deletion tool. Deletion is a manual database operation for the operator; a reviewed tool that deletes the account data and keeps the records listed above would be a follow-up.
* Stopping new entries for a customer who has not accepted a new version. Today new entries continue under the customer's settings (engine independence was the safer choice). If required, it must be a new-entry-only check that can never touch an open trade.
* A customer-facing "Cancel subscription" button. Cancelling a card subscription is by Paystack's own link or by e-mail to the operator; the Terms say so.
* Automatic refunds. Refunds are decided and made by the operator through the payment provider.

## 6. UNVERIFIED
* That Paystack's e-mails to a subscriber carry a manage-subscription link (the Terms mention it). Check one real e-mail.
* Paystack and NOWPayments refund behaviour, timing and fees (the Terms promise only that an approved refund is made through the same provider and method).
* Governing law: the Terms say the laws of the Federal Republic of Nigeria; that is the operator's call to confirm.
