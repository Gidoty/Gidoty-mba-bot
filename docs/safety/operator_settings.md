# Operator settings table and admin actions

Status: built, OFF. Nothing changes until the owner sets `ADMIN_ACTIONS_ENABLED=true` (and the admin dashboard is already on). With the switch off every write route answers 403 and the dashboard stays read only. **Depends on nothing** (based on main). **No migration**: the three new tables (`operator_settings`, `admin_changes`, `customer_notes`) are created on start by `init_db()` (`create_all`), like the other additive tables; the old code ignores them, so it keeps running until the restart. Until the tables exist, every reader falls back to the environment value.

## The settings table
`operator_settings` holds NON-SECRET operator switches. Rules, enforced in `operator_settings.py` and tested:
* An explicit allowlist (`ALLOWLIST`): name, type (boolean, bounded number, or one word from a fixed list), bounds, code default, restart needed or not, customer visible or not. Any other key is refused. There is no free-text type, so a secret cannot be typed in; a test fails if a key name contains a secret word (key, secret, token, password, webhook, ssh, age, ...).
* Secrets (API keys, tokens, passwords, webhook secrets, SSH or age material) are never in the table and cannot be edited there. They stay in the environment, entered by the owner.
* One reader: `get_setting(key, base_settings)` returns the database value if there is a valid one, else the environment value. A missing row, a missing table, an unreadable database and an invalid stored value all give the environment value, so the `.env` keeps working and an empty table changes nothing. `effective_settings(settings)` applies the rows to a whole `Settings` object (the API does this on every request, cached 5 seconds; the scheduler does it at the start of each round).
* Deliberately NOT on the allowlist: anything that decides or manages a live trade (the live strategy switches, the closed-candle switch, entry windows, protection and guard modes, the kill switch file). Those stay environment-only. The kill FLAG has its own action (below).

| key | type and bounds | default | needs a restart | customer can see it |
|---|---|---|---|---|
| signups_mode | open or closed | open | no (next request) | yes |
| setup_advisor | true or false | false | no (next request) | yes |
| poll_interval_seconds | 60 to 3600 | 300 | no (next round) | no |
| subscription_grace_days | 0 to 30 | 5 | no (next request or round) | no |
| login_max_failures | 3 to 20 | 5 | no (next request) | no |
| login_lockout_seconds | 60 to 86400 | 900 | no (next request) | no |
| rate_limit_per_minute | 10 to 1000 | 120 | **yes** (the middleware is built at start) | no |
| rate_limit_auth_per_minute | 3 to 200 | 10 | **yes** | no |
| disable_api_docs | true or false | false | **yes** (the app is built at start) | no |

"Without a restart" means the next API request or the next scheduler round (at most one poll interval). A restart-needed value is stored at once and applied at the next start; the dashboard says "stored; takes effect at the next restart" and still shows the environment value as the one in effect.

## Admin actions
All behind the existing admin sign-in AND a fresh authenticator code on every call (step-up: the same checker as the sign-in, so five wrong codes lock it for 15 minutes and a code used once cannot be used again; wait for the next code). Every call needs a reason of 5 to 200 characters, is limited to 30 a minute, writes one `admin_changes` row (who, when, old, new, reason) in the same transaction as the change, and also leaves the normal `admin_audit` row.

| route (POST, under /api/admin) | what it changes, and nothing else |
|---|---|
| settings/set, settings/reset | one `operator_settings` row |
| customers/{ref}/pause, resume | `trading_accounts.is_active` (no new entries; open trades stay managed) |
| customers/{ref}/suspend, reinstate | `customers.is_active` (sign-in) and the bot switch; reinstate leaves the bot paused |
| customers/{ref}/subscription/extend, end | the `subscriptions` row (manual provider if new). Database only: a Paystack or NOWPayments subscription is not cancelled or charged by it |
| customers/{ref}/note | one `customer_notes` row, admin eyes only |
| requests/{id}/approve, refuse | a data request: `open` to `approved` or `declined`. Nothing is deleted or exported by this (the closure flow is a separate part) |
| kill-switch/engage, release | the existing kill flag through `kill_switch.set_state` (one row, one kill-switch event) |

`{ref}` is the account id shown as `ref` in the customer list. Nothing here places, closes or modifies a trade or an exchange order, calls an exchange or a payment provider, or contacts a customer (a test reads the module for such calls and checks a trade row is unchanged after every action).

An open trade is always managed: `db.list_active_accounts` lists an inactive account that has an open trade, so pausing or suspending a customer never abandons a live position.

## Owner steps (nothing here is run by Claude)
1. Deploy when no live trade is open (restart gate). No migration is needed.
2. To use the write routes: set `ADMIN_ACTIONS_ENABLED=true` in the environment and restart. Leave it unset to keep the dashboard read only.
3. Rollback: unset the variable (writes stop at once after a restart); values stored in `operator_settings` stay until reset from the dashboard (or delete the rows); the change log is kept.

## Not proven
Fake exchange and an isolated database stand in for the real ones. The new panel was built with `vite build` but not looked at in a browser. Behind several workers each process keeps its own 5-second cache, so a change can take up to 5 seconds longer to show in another worker (the live deployment runs one).
