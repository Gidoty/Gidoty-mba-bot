# Customer leverage setting and size-cap mode (B6a)

Status: ships OFF. `FUTURES_LEVERAGE_MODE` defaults to `strict`, which behaves exactly as on `main`. CLAUDE-RUN: tests pass in the sandbox against a FAKE exchange. Nothing is deployed, no migration has been run, no order has been placed. One code path for every account: no account, e-mail or owner is named or branched on anywhere.

## What it does

* New per-account number `trading_accounts.max_leverage`: a whole number 1 to 5, read as 3 when empty, never above `FUTURES_MAX_LEVERAGE` (the global hard limit, when set). The customer edits it in the dashboard (Futures only) and in Telegram (`/leverage`, `/leverage 2`). It applies to NEW entries only: it is read when the bot sizes an entry, so a trade already open is never touched. Spot accepts and ignores it.
* `FUTURES_LEVERAGE_MODE`:
  * `strict` (default): unchanged. A new Futures entry is refused when the pair's Binance leverage is above `FUTURES_MAX_LEVERAGE` or the margin mode is not isolated. The customer's number is not used.
  * `size_cap`: a high Binance number does not refuse the entry. The position value is capped at `effective x capital`, `effective = min(Binance leverage for the pair, the account's number)`, using the existing `apply_margin_ceiling`. Binance below the customer's number: sized to Binance (margin-bound), as today. Binance above: sized to the customer's number.
* In `size_cap`: isolated margin is still required. A pair on cross, or a margin mode that cannot be read, is refused with a plain message that says how to fix it on Binance (`docs/customer/leverage_help.md`).
* The liquidation check (the pre-entry protection check) is not changed and still judges the stop against the REAL Binance leverage and the liquidation price built from it, not the customer's number. A test proves it: Binance 20x with a customer 3x opens with position value <= 3 x capital on a tight stop, and a stop 7.8% from entry (beyond the safe distance for 20x, far inside liquidation at 3x) is still refused; Binance 125x is refused on a 0.5% stop.
* `size_cap` only takes effect when `PROTECTION_PRECHECK_MODE=enforce`. Otherwise it behaves as `strict` and logs one line per hour saying why (`FUTURES_LEVERAGE_MODE=size_cap needs PROTECTION_PRECHECK_MODE=enforce (it is observe); behaving as strict`). It never runs size_cap without the liquidation check.
* The bot only READS leverage and margin mode. It never sets either on Binance.

## How the current code reads Binance's leverage and margin mode

`exchange.fetch_symbol_leverage_and_margin_mode` calls ccxt's `fetch_leverage(symbol)`. In ccxt 4.5.88 (pinned) that calls `fetch_leverages([symbol])`, which for USD-M is a GET on Binance's symbol-config endpoint (`fapiPrivateGetSymbolConfig`), and `parse_leverage` turns each entry into `{marginMode, longLeverage, shortLeverage}`: `marginMode` is `cross` when the entry's `marginType` is `crossed` and `isolated` for any other value, or `None` when neither `isolated` nor `marginType` is present; the leverage is the entry's `leverage` as an integer. The bot treats an absent or non-numeric leverage as unreadable (entry refused) and a `None` margin mode as not isolated (refused). **This was checked against ccxt's source in this sandbox, NOT against a live Binance reply.**

## Owner steps (the owner runs every one; nothing here is done by the code)

1. Run `python -m mba_bot.restart_gate` (exit 0 = clear; 3 = a live trade is open, do NOT stop the service; 2 = unreadable, do not stop).
2. Stop the service.
3. Back up the database.
4. `alembic upgrade head` (applies `b9e3d7a2c5f1`: one nullable column on `trading_accounts`, no backfill).
5. Deploy the code.
6. Start the service. Check health and the journal. Nothing changes: the mode is still `strict`, and the dashboard shows the new Leverage control for Futures accounts.
7. To switch on `size_cap` (one switch, its own restart; restart gate first, never restart with a live trade open): confirm `PROTECTION_PRECHECK_MODE=enforce`, set `FUTURES_LEVERAGE_MODE=size_cap`, restart, check the journal for the "behaving as strict" line (it must NOT appear).

Rollback: set `FUTURES_LEVERAGE_MODE=strict` (or remove the line), restart gate, restart. The column can stay. To remove it: `alembic downgrade a8d2f6c1e3b7` (every account is then read as 3).

## NOT verified

* A live Binance reply for leverage and margin mode (above). Whether Binance's symbol-config endpoint is per symbol exactly as ccxt's parser assumes; whether margin mode and leverage can be changed while a position is open (not needed by the bot).
* Any exact click path on Binance's screens: `docs/customer/leverage_help.md` is generic and marks the rest UNVERIFIED.
* The dashboard info icons and Telegram `/help` are not built; the wording is waiting for review.
* The fake exchange proves control flow only.
