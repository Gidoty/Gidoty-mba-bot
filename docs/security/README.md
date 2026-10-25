# Security hardening (steps 1 and 2)

Step 1 (`e4dd826`, deployed) added the switches below. Step 2 (branch `security/step2-paystack-reference`, **not deployed**) adds the issued-reference record and
the `log` mode for Paystack verification. Every behaviour change is behind a switch that is OFF or UNSET by default; with all of them off the application behaves exactly
as before (pinned by tests). Switches are environment variables that only the owner sets.

## Production state (owner-confirmed on the VPS, 2026-10-25)

* Deployed commit `e4dd826` on `main`. One uvicorn process (`127.0.0.1:8000`) behind Nginx and Cloudflare. Zero errors since restart; the scheduler runs a round every 5 minutes with all active accounts ok.
* ON: `API_SINGLE_INSTANCE_LOCK=1`, `LOGIN_THROTTLE_ENABLED=1`, `RATE_LIMIT_ENABLED=1`. Login check: 5 failures return 401, the 6th returns 429.
* OFF or unset: `PAYSTACK_STRICT_VERIFY`, `TELEGRAM_WEBHOOK_SECRET`, `DISABLE_API_DOCS` (present but empty, which parses as off), `SQLITE_WAL`.
* `payment_events` exists and is empty. File permissions: `.env` and `.env.save` 600; database and backups 600/700.
* No Telegram webhook traffic in the last 24 hours, so the Telegram secret switch stays off for now.

## The switches

| Item | Switch (env var) | Default | What it does when on | Rollback |
|---|---|---|---|---|
| F-01 Paystack | `PAYSTACK_STRICT_VERIFY=off\|log\|enforce` (`1` means enforce) | off | **log**: runs the full verification and writes one log line per charge event, but accepts and rejects exactly as before. **enforce**: each `charge.success` must match a reference this application issued to that customer; Paystack's own record must say success, same reference, same currency, a price-consistent amount (see below), same customer; each accepted event is recorded once in `payment_events`. Anything else grants nothing. Paystack unreachable: HTTP 503 so Paystack retries (enforce only). | Unset, or set to `off`, restart. |
| F-03 Lock | `API_SINGLE_INSTANCE_LOCK=1` | **on** | The API takes the same single-instance file lock as `main.py` (`MBA_BOT_LOCK_FILE`). A second worker or a second copy of the service refuses to start. | Unset, restart. |
| F-05 Telegram | `TELEGRAM_WEBHOOK_SECRET=<value>` | unset | Webhook requires header `X-Telegram-Bot-Api-Secret-Token` equal to it (constant-time compare). Value rules (Telegram): 1 to 256 characters of `A-Z a-z 0-9 _ -`. | Unset, restart (the header Telegram sends is then ignored). |
| F-04 Limits | `RATE_LIMIT_ENABLED=1`, `LOGIN_THROTTLE_ENABLED=1` (+ `RATE_LIMIT_PER_MINUTE=120`, `RATE_LIMIT_AUTH_PER_MINUTE=10`, `LOGIN_MAX_FAILURES=5`, `LOGIN_LOCKOUT_SECONDS=900`) | **on** | In-app per-address limits (login/signup stricter) and a failed-login lockout per (address, email) plus a cap of four times that per address. Provider webhooks and `/api/health` are never limited. State is in memory: a restart clears it. | Unset the two, restart. |
| F-06 Docs | `DISABLE_API_DOCS=1` | off | `/docs`, `/redoc`, `/openapi.json` return 404. | Unset, restart. |
| WAL | `SQLITE_WAL=1` (+ `SQLITE_BUSY_TIMEOUT_MS=15000`) | off | **Prepared only.** See `wal_runbook.md`. | Its own runbook (the journal mode is stored in the file). |

## Runbook entry: the client address behind Cloudflare and Nginx

**Finding.** The site config set only `X-Real-IP`. The app reads the client address that uvicorn derives from `X-Forwarded-For`, and uvicorn trusts that header when the request comes from
`127.0.0.1` (Nginx). A client-supplied `X-Forwarded-For` sent straight to the origin therefore became the address the rate limiter and login throttle used: an attacker could evade the limits or
lock another person's address out. Through Cloudflare without a fix, every visitor would also have appeared as a Cloudflare address.

**Fix (applied on the server by the owner).**
1. `conf.d/cloudflare-realip.conf`: one `set_real_ip_from <range>;` line per Cloudflare range (the published IPv4 and IPv6 lists) and `real_ip_header CF-Connecting-IP;`. Nginx now treats `CF-Connecting-IP` as the client address only for requests that really come from Cloudflare.
2. In the API location: `proxy_set_header X-Forwarded-For $remote_addr;` (an overwrite, not `$proxy_add_x_forwarded_for`), so uvicorn only ever sees the address Nginx determined.

**Verify (read-only, repeatable).** Send a forged header directly to the origin and confirm the app does not use it:
`curl -sk --resolve bot.metabridgeacademy.com:443:<ORIGIN_IP> -H 'X-Forwarded-For: 203.0.113.9' https://bot.metabridgeacademy.com/api/health`, then
`journalctl -u mba-bot -n 5 --no-pager | grep "GET /api/health"`: the address logged must be your own, not `203.0.113.9`. Then the same through Cloudflare from your laptop: the logged address must be your public IP.

**Upkeep.** Cloudflare changes its ranges rarely; re-check https://www.cloudflare.com/ips/ when you review Nginx and after any Cloudflare notice. A stale list makes new Cloudflare addresses look like ordinary clients. Optionally, allow only
Cloudflare ranges to reach port 443 on the origin firewall, so nothing can bypass Cloudflare at all.

**Rollback.** Restore the backed-up Nginx files, `sudo nginx -t`, `sudo systemctl reload nginx`. Turn the two in-app limit switches off first if the address check fails, so real customers are not throttled together.

## Paystack: issued references and the three modes (step 2)

**Problem.** `/initialize` overwrote `customer.paystack_reference` on every call. With enforcement on, a customer who started checkout twice and paid the first link would be refused (`reason=reference_not_issued`).

**Design chosen: a small table of issued references (`payment_references`).** The existing column is untouched and keeps its old behaviour (latest reference).
In `log` and `enforce` modes `/initialize` also records each reference with its customer, price and currency. The webhook then accepts a reference if it was issued to that customer (any of them), or, for
references from before the table existed, if it equals the customer's latest stored reference (as before). The recorded price is the one the amount is checked against, so changing `PAYSTACK_AMOUNT`
later does not strand a payment that was started at the old price.

| | (a) HMAC inside the reference | (b) table of issued references (chosen) |
|---|---|---|
| Schema change | none | one additive table |
| Secrets | needs a signing key: reuse of `PAYSTACK_SECRET_KEY`/`JWT_SECRET` (rotation breaks links in flight, and the Paystack key was rotated before) or a NEW env variable | none |
| Price at issue time | cannot be bound without more fields in the reference | stored, so a later price change is safe |
| Audit and reconciliation | none | every issued link is listed; the table seeds the billing redesign |
| Revocation, expiry | needs a timestamp in the reference | trivial (delete or add a column later) |
| Failure mode | key mix-up refuses valid payers | a failed insert is logged and ignored; falls back to the latest-reference rule |

**Deploy impact.** `init_db()` runs `create_all()`, so the empty `payment_references` table is created at the first service start after deploy, even with the switch off (same as `payment_events` was). It is additive,
unused while the mode is `off`, and needs no existing column to change. `proposed_migration_payment_references.py` (here, not in `alembic/versions/`) is the idempotent migration for it; no new environment variable is needed.

**Log mode.** Per charge event: one line, `paystack verify[log]: verdict=accept|reject|unavailable|duplicate reason=<code> today=grants|ignores customer=<first 8 chars>... ref=...<last 6 chars>`. No email, no secret, no raw Paystack
payload. It runs after today's handling and is time-boxed (6 s); any error inside it is swallowed, so it cannot change an outcome or a response. `today=grants verdict=reject` lines are the ones that enforcement would have blocked: review each before moving on.

**Amount rule (enforce and log).** The price is the recorded issue-time price (else `PAYSTACK_AMOUNT`). `amount` may equal it (business pays) or equal price + `fees` (customer pays); `requested_amount` is the price when present.
Underpaid, extra not explained by `fees`, non-integer or implausible figures are refused. The amount alone never grants access: status, reference, currency, customer and issued-reference are checked as well. Fee arithmetic allows 1 minor unit; the price is compared exactly.

**Monitoring.** `journalctl -u mba-bot | grep -E "paystack verify\[log\]|paystack webhook rejected|paystack verify unavailable"`.
**Recovery for a refused genuine payer.** `scripts/mark_customer_paid.py` (existing; grants access by hand).

## Order of enabling the remaining switches (one at a time; restart after each; each reversible)

1. **Deploy step 2 with the mode still `off`.** Check health, journal, and that `payment_references` exists and is empty.
2. `DISABLE_API_DOCS=1`: `/docs` and `/openapi.json` must return 404.
3. `TELEGRAM_WEBHOOK_SECRET` when you want it: check `getWebhookInfo` first (if no webhook is registered, there is nothing to protect yet). Register the secret with Telegram **first**, then set the variable. Test: a bare POST returns 403 and the link flow still works.
4. `PAYSTACK_STRICT_VERIFY=log`. Let at least one real payment (and ideally a customer's second checkout) pass. Read every `paystack verify[log]` line. Move on only when real payments show `verdict=accept` and any `reject` is understood.
5. `PAYSTACK_STRICT_VERIFY=enforce` in a quiet period, `scripts/mark_customer_paid.py` ready, the monitoring command above open. After the next real payment confirm the customer is unlocked and one `payment_events` row exists.
6. WAL: separate approval, `wal_runbook.md`.

## Database

* `payment_events` (step 1) exists and is empty. `payment_references` (step 2) is created at the first start of step 2. Both are additive; neither is read or written while the mode is `off`.
* Neither migration is installed in `alembic/versions/`. They are idempotent proposals; do not run them unless you decide to. `alembic stamp` is never needed.
* Alembic heads: production is at `c7d2e5f8a1b3`. The telemetry migration `d4a8f1c3e6b2` (research branch) and `proposed_migration_payment_events.py` (`e1f7a3c9b5d2`) both name `c7d2e5f8a1b3` as parent: installing both as written gives TWO heads.
  Chain them instead: `c7d2e5f8a1b3` -> `d4a8f1c3e6b2` -> `e1f7a3c9b5d2` -> `f2a8b4d0c6e3` (this branch's proposal), by editing `down_revision` at install time. Do this only when you decide to install them.

## Rollback for the whole of step 2

Set the mode back to `off` (or unset) and restart; or check out `e4dd826` and restart. The `payment_references` table can stay (empty or not, it is unused while the mode is `off`).

## Risks to existing customers

* Everything off: none identified; the existing tests pass unchanged and the new tests assert the off-state.
* `log`: it adds one Paystack API call per charge event and a log line, time-boxed at 6 seconds; the webhook response is still 200 and the outcome identical (tested for eleven kinds of event).
* `enforce`: a genuine payer could be refused if Paystack's figures differ from the assumptions (reason logged; fix by hand). References issued before step 2 are honoured only through the latest one. Existing customers cannot lose access: the code never lowers `has_paid`.
* Limits on: the address check above must stay valid. If Cloudflare ranges go stale, new Cloudflare addresses look like ordinary clients and share limits.
