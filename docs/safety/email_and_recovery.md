# Email sender, email verification and password reset

Status: built, OFF. Nothing is sent until the owner chooses a transport and enters its credentials in the environment. **Depends on nothing** (based on main). **No migration**: the two new tables (`email_tokens`, `customer_email_state`) are created on start by `init_db()`; the old code ignores them, so it keeps running until the restart.

## The sender (`mailer.py`)
One interface, two transports, chosen by `EMAIL_TRANSPORT` (`off` default, `smtp`, `http`; anything else is `off`, so a typo never starts sending mail).
* `smtp`: `EMAIL_FROM`, `EMAIL_SMTP_HOST`, `EMAIL_SMTP_PORT` (587), `EMAIL_SMTP_USER`, `EMAIL_SMTP_PASSWORD`, `EMAIL_SMTP_SECURITY` (`starttls` default, `ssl`, `none`).
* `http`: `EMAIL_FROM`, `EMAIL_HTTP_URL` (must be https) and `EMAIL_HTTP_KEY`. One POST with `Authorization: Bearer <key>` and JSON `{from, to:[...], subject, text}`. The shape is UNVERIFIED against any provider: pick a provider, then check its API against this and adjust `HttpMailer` if needed.
* The credentials are secrets: **the owner enters them** in the environment file; they are never in the database, never in the repository, never returned by a route and not in the settings `repr`. A half set up transport counts as off.
* `send` never raises and logs only a class name (no address, no body, no secret). A subject is flattened to one line and an invalid address is refused before any connection, so a header cannot be injected.
* Other optional settings: `EMAIL_RESET_TTL_MINUTES` (30, 5 to 120) and `EMAIL_VERIFY_TTL_HOURS` (48, 1 to 168). The link base is `FRONTEND_ORIGIN`.

## Password reset
* `POST /api/auth/password-reset/request {email}`: with email off it answers 503 "Email is not set up..." and the sign-in screen shows that. With email on it answers the SAME words for an address with an account, one without, and a suspended one. The request itself does no database work: the lookup, token and mail happen after the response, so the answer and its time do not depend on the address. Limits: 5 requests a minute per caller address (429 for everyone alike), and 3 an hour per email address (silent: same answer, no mail), plus at most 3 tokens an hour per customer.
* The token is 256 random bits, only its SHA-256 is stored, it expires (30 minutes), works once (one atomic update) and a newer request cancels the older link. The link puts the token in the URL fragment (`#/reset-password/<token>`), which is not sent to a server and not in access logs.
* `POST /api/auth/password-reset/confirm {token, new_password}` (8 to 72 characters, as at signup): unknown, used, expired, wrong-purpose, superseded and suspended-customer tokens all answer the same 400. It changes the password hash and nothing else: **consent is unchanged** (no legal acceptance row is read or written; after the reset the customer signs in and the existing consent gate applies as before). It does not sign the customer in.
* A successful reset ends every session issued before it. The reset stores the moment it happened (`customer_email_state.sessions_valid_after`, a column of the table this change adds, so no migration) in the same commit as the new password; a session token carries its issue time in microseconds (`iat_us`) and is refused with 401 when it was issued before that moment (`api/deps.py`). A session opened after the reset works. A refused link, a link request or a verification ends nothing. It works for a customer who has no session. A token issued before this change has only whole seconds and counts as older. The cost is one primary-key read of that table on each authenticated request. The password reset does not sign the customer in. An old database that already has this table from an earlier build of this branch would need the column added by hand; none is deployed.

## Email verification
`POST /api/auth/email/verify/request` (signed in) sends a link; `POST /api/auth/email/verify/confirm {token}` marks the address verified (`customer_email_state.verified_at`). `GET /api/auth/email/status` says whether email is available, whether this address is verified and the opt-out. Verification gates nothing today: no feature requires a verified address.

## Notices and the opt-out
`email_flows.send_notice` sends a billing or system notice through the same sender. Essential notices (verification, reset, "your subscription period has ended") are always sent; non-essential ones (the renewal reminder) are not sent to a customer who ticked "Do not send me reminders and product notices" (`POST /api/auth/email/preferences`). The Telegram messages are unchanged; the email is an extra copy, and only when email is set up. A notice is never sent to a suspended customer.

## What is not covered
* Whether a real mail server or provider delivers, or whether a message lands in spam (SPF, DKIM, DMARC for the sending domain are the owner's DNS to set up).
* `signup` still answers 409 for an address that already has an account; that is the existing behaviour and is not changed here.
* A one-click unsubscribe header is not added.

## Owner steps (nothing here is run by Claude)
1. Deploy when no live trade is open. No migration.
2. Choose a transport, set the variables above in the environment file yourself, restart. Until then every screen says email is not set up.
3. Rollback: set `EMAIL_TRANSPORT=off` and restart. Tokens already issued simply expire.
