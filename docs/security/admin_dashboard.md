# Admin dashboard (read only)

Ships behind `ADMIN_DASHBOARD_ENABLED` (default off). Off: the admin routes are not mounted (every `/api/admin/...` path is a plain 404), the Admin link and page do not appear, and every other screen and
API response is exactly as before. The only trace is that the app asks `GET /api/admin/me` once per page load and gets a 404.

## What it is

A page for the owner, who is the only admin. Read only: it cannot start, stop, change or delete anything. It writes only its own audit rows and the sign-in counters.

| Panel | Source | Query |
|---|---|---|
| Customers: total, active, paid, new in 7 and 30 days | `customers` | one conditional-sum query |
| Bots by mode: practice, live, live but stopped, inactive | `trading_accounts` (+ `customers`, `api_credentials`) | one GROUP BY over the seven inputs of the mode rule, then `onboarding.resolve_trading_mode` once per distinct combination |
| Health: oldest last scan, bots with an error now, bots not scanned in 15 minutes, open trades, live trades whose protection is not `protected`, kill switch state, deploy commit | `trading_accounts`, `trades`, `kill_switch` | one query each |
| Results per bot, live, demo and practice separate, by strategy | `trades` | two grouped queries (totals, and the 50 bots with the most closed trades); built into `reporting.Group`, so gross, net and the labels are the reporting module's |
| Customer list, 50 per page, emails masked | `customers` + `trading_accounts` | count + one page query |
| Skipped pairs by reason, last 7 days | `pair_skips` | one GROUP BY |
| Billing (read only): subscriptions by state, expiring in 7 days, payments in 30 days by provider and status, failed or mismatched count, which billing switches are on | `subscriptions`, `billing_payments`, settings | two queries; states come from `billing.entitlement.subscription_state` |

Every panel is cached in memory for 60 seconds. A request makes at most 5 statements in total (the customer lookup, the panel, the audit row); a statement-counting test enforces it. Results are before fees
unless every fee of a group is known (`net`), practice is labelled simulated, exactly as `docs/safety/reporting.md`.

"Live but stopped" means: paid, live trading enabled, a mainnet key on file, and the bot not started (it would be live once started). The demo runner is not counted separately.
"Bots with an error now" counts active bots whose `last_error` is set (the bot clears it on a good cycle). The error text is never shown, because it can hold text from the exchange.

## Who is an admin

A customer whose email is in the environment list `ADMIN_EMAILS` (comma separated). Never a database column. They sign in normally, then enter a 6-digit code from an authenticator app (or a recovery code):

* admin session: its own cookie `mba_admin`, path `/api/admin`, signed with a key derived from `JWT_SECRET` so a normal session token is never an admin token; 15 minutes from the last request;
* every request by an admin is written to `admin_audit` (action, path, HTTP status, short detail; no email, no address). If the row cannot be written the request is refused (503) and no data is returned;
* code: 30-second steps, one step of drift, a step can be used once (`last_step`, updated atomically); 5 wrong codes lock the admin sign-in for 15 minutes (stored, so a restart does not clear it); the sign-in is also limited to 10 attempts a minute per client address;
* recovery codes: 10, each 16 characters, stored as salted SHA-256 digests, removed when used;
* the TOTP secret is encrypted with the same Fernet key as the exchange credentials (`ENCRYPTION_KEY`); no route returns it;
* removing an email from `ADMIN_EMAILS` (or deactivating the customer) ends access on the next request;
* no request header is read for any decision. The address used for the rate limit is `request.client.host`, which uvicorn only replaces from `X-Forwarded-For` for the trusted local proxy.

Masking: first character, three stars, domain (`a***@gmail.com`). Reveal: a reason of 5 to 200 characters, written to the audit log in the same transaction, at most 20 a day per admin, returns the email once.
Never returned by any route: password hash, API keys or secrets (encrypted or not), tokens, Telegram chat ids, webhook secrets, payment references, addresses. Response models list their fields explicitly, and a test
serialises every response and fails on a forbidden name, a planted value or an email-like string outside the reveal.

New dependency: `pyotp==2.9.0` (exact pin), a small pure-Python implementation of RFC 6238 with no dependencies. Upgrade deliberately and re-run `tests/test_admin_dashboard.py`.

## Owner steps (nothing here is run by Claude)

1. **Check WAL first.** The 11 Nov handoff notes say WAL is on in production (reported by the owner). The code default and the docs (`wal_runbook.md`, `.env.example`) still say prepared, not deployed, so check:
   `sqlite3 'file:/opt/mba-bot/mba_bot.db?mode=ro' "pragma journal_mode;"` must print `wal`. Without WAL, keep the dashboard off during busy periods.
2. Back up the database (online `.backup`), then run the migration after the restart gate: `alembic upgrade head` (adds `admin_totp` and `admin_audit`; head `a1d6c3e8b5f7`). Install the pinned library: `pip install -r requirements.txt`.
3. Set `ADMIN_EMAILS=you@example.com` in the environment (your own customer email, already signed up).
4. Enrol your authenticator on the server: `cd /opt/mba-bot && PYTHONPATH=src .venv/bin/python -m mba_bot.admin_enroll --email you@example.com`. It prints the setup key and 10 recovery codes once. Type the key into your authenticator app and keep the recovery codes offline. `--replace` starts over.
5. Set `ADMIN_DASHBOARD_ENABLED=1`, run the restart gate, restart.
6. Rebuild the frontend (it is served from Vercel). Sign in, open the Admin link (sidebar, or More on a phone), enter the code.

Optional: `DEPLOY_COMMIT=<short commit>` in the environment sets the "last deploy" shown (otherwise it reads the checkout's `.git`; UNVERIFIED that the server checkout has one).

## Roll back

Remove `ADMIN_DASHBOARD_ENABLED` (or set 0) and restart: the routes disappear. `ADMIN_EMAILS` can stay. To remove the tables too: `alembic downgrade d9b5f7c2e4a6` (the enrolment and the audit history are lost).

## Not covered

Nginx or Cloudflare rules for `/api/admin` (UNVERIFIED; a rate limit there is a second line), timing on the production server, and writes of any kind (v1 has none).
