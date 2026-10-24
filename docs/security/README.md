# Security hardening, step 1 (branch `security/step1-hardening`)

Based on production `main` (`0b1d415`). **Nothing here is deployed.** Every behaviour change is behind a switch that is OFF or UNSET by default;
with all of them off the application behaves exactly as before (pinned by tests). Switches are environment variables that only the owner sets.

| Item | Switch (env var) | Default | What it does when on |
|---|---|---|---|
| F-01 Paystack | `PAYSTACK_STRICT_VERIFY=1` | off | Each `charge.success` is checked: reference must be the one issued at `/initialize` for that customer; Paystack's own record (`/transaction/verify`) must say success, same reference, same currency as `PAYSTACK_CURRENCY`, amount equal to `PAYSTACK_AMOUNT`, same customer in its metadata; each accepted event is recorded once in `payment_events` (unique constraint). Anything else grants nothing and is logged with a short reason code. Paystack unreachable: HTTP 503 so Paystack retries; nothing recorded. |
| F-03 Lock | `API_SINGLE_INSTANCE_LOCK=1` | off | The API takes the same single-instance file lock as `main.py` (`MBA_BOT_LOCK_FILE`). A second worker or a second copy of the service refuses to start. |
| F-05 Telegram | `TELEGRAM_WEBHOOK_SECRET=<value>` | unset | Webhook requires header `X-Telegram-Bot-Api-Secret-Token` equal to it (constant-time compare). Unset = accept all, as today. Value rules (Telegram): 1 to 256 characters of `A-Z a-z 0-9 _ -`. |
| F-04 Limits | `RATE_LIMIT_ENABLED=1`, `LOGIN_THROTTLE_ENABLED=1` (+ `RATE_LIMIT_PER_MINUTE=120`, `RATE_LIMIT_AUTH_PER_MINUTE=10`, `LOGIN_MAX_FAILURES=5`, `LOGIN_LOCKOUT_SECONDS=900`) | off | In-app per-address limits (login/signup stricter), and a failed-login lockout per (address, email) plus a cap per address. Provider webhooks and `/api/health` are never limited. Nginx limits are separate: `nginx_rate_limit.md`. |
| F-06 Docs | `DISABLE_API_DOCS=1` | off | `/docs`, `/redoc`, `/openapi.json` return 404. |
| WAL | `SQLITE_WAL=1` (+ `SQLITE_BUSY_TIMEOUT_MS=15000`) | off | **Prepared only.** See `wal_runbook.md`. Do not enable without its steps. |

Files: `src/mba_bot/config.py`, `db.py`, `main.py`, `log_safety.py`, `models.py` (new `PaymentEvent`), `api/app.py`, `api/ratelimit.py` (new),
`api/routers/billing.py`, `auth.py`, `telegram.py`; tests in `tests/test_security_hardening.py`. No new dependency.

## Database

F-01 adds one table, `payment_events`. The migration is NOT in `alembic/versions/`: the proposed file is
`proposed_migration_payment_events.py` (here). Two facts to know before approving it:

1. `init_db()` runs `Base.metadata.create_all()` at every service start (existing behaviour). So on the first start of this code the empty
   table is created even with every switch off. It is additive and unused until `PAYSTACK_STRICT_VERIFY` is on. The proposed migration is idempotent
   (does nothing if the table exists), so the order does not matter, and it never needs `alembic stamp`.
2. The research branch has a telemetry migration with the same parent (`c7d2e5f8a1b3`). Merging both gives two Alembic heads and needs a merge revision.

## Order of enabling (one at a time; each is reversible by unsetting the variable and restarting)

1. `DISABLE_API_DOCS=1`: check `https://<api>/docs` returns 404 and the site works.
2. `API_SINGLE_INSTANCE_LOCK=1`: check the service starts and `fuser`/`lsof` shows one holder of the lock file; try `python -m mba_bot.main` by hand: it must refuse.
3. `TELEGRAM_WEBHOOK_SECRET`: **first** register the secret with Telegram (so Telegram starts sending the header), **then** set the variable and restart. See below.
4. Nginx limits (`nginx_rate_limit.md`), then `LOGIN_THROTTLE_ENABLED=1`, then `RATE_LIMIT_ENABLED=1` only after confirming the app sees real client addresses.
5. `PAYSTACK_STRICT_VERIFY=1` last, in a quiet period, with `scripts/mark_customer_paid.py` ready (see below).

### Telegram secret: the order matters

Telegram accepts the secret when you register the webhook. With the bot token in your shell (do not paste it anywhere):
`curl -sS "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" -d url=https://<api>/api/telegram/webhook -d secret_token="$NEW_SECRET"`
(this keeps the same URL; it only adds the header). While the app has no secret configured it ignores the header, so there is no gap. Then set
`TELEGRAM_WEBHOOK_SECRET` to the same value and restart. Test: `curl -i -X POST https://<api>/api/telegram/webhook -H 'content-type: application/json' -d '{}'` must now return 403, and your bot link flow must still work.
Rollback: unset the variable and restart (the header Telegram sends is then ignored).

### Paystack strict mode: what to expect and how to fall back

* Verified against Paystack's response fields `status`, `reference`, `currency`, `amount`, `requested_amount` (if present) and `metadata.customer_id`.
  If your Paystack account makes the customer pay the fees, Paystack's `amount` is larger than the price while `requested_amount` equals it; both cases are accepted. **Please confirm
  with a real transaction record that these fields look as assumed before relying on this**; I cannot call Paystack from here.
* One known limit: if a customer starts a second checkout and then pays the FIRST one, the stored reference is the second, so the payment is refused (logged
  as `reason=reference_not_issued`). Fail-safe direction (no free access), and you can grant manually with `scripts/mark_customer_paid.py`.
* Look for rejections: `journalctl -u mba-bot | grep "paystack webhook rejected"` (reason codes only, no secrets).
* Existing paid and hand-approved customers are untouched: the code never lowers `has_paid`, and an event for an already-paid customer is recorded as `already_paid` and changes nothing.

## Rollback (any item)

* Unset the variable (or set it to 0) and restart the service. That restores today's behaviour for that item.
* Whole branch: `git checkout <previous production commit>` on the VPS and restart. The `payment_events` table can stay (harmless, unused); the proposed migration's
  downgrade drops it if you ever want it gone. WAL rollback has its own steps in `wal_runbook.md`.

## Risks to existing customers

* All switches off: none identified; the 565 pre-existing tests pass unchanged and the new tests assert the off-state.
* F-01 on: a genuine payer could be refused (see the known limit, or if Paystack's fields differ from the assumption). They would not be charged more or less; they would
  not be unlocked until you grant access. Existing customers cannot lose access through this code.
* F-04 on: if the app does not see real client addresses (all requests from the local proxy), everyone shares one limit. Verify before enabling (see Nginx doc).
* F-03 on: a service restart that overlaps the old process would refuse to start until the old one exits; systemd stops the old process first, so this is the intended behaviour.
