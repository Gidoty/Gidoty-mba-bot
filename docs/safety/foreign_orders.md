# Foreign-order alarm

Status: on a branch (PR 2), not merged, not deployed. Ships OFF.

## What it does
When `FOREIGN_ORDER_ALARM=alert`, the scheduler looks at each LIVE account about every `FOREIGN_ORDER_CHECK_MINUTES` (default 15, clamped 5 to 240) and sends one Telegram alert if the exchange shows:
* an open order whose client id does not start with the bot prefix (`mba`), or
* (Futures only) an open position on a pair where the bot has no non-terminal live trade.

Pairs checked: the account's pair, its manual pairs and the pairs of its open live trades.
Spot balances are not checked (a Spot wallet always holds assets the bot did not buy).

## What it never does
It does not cancel, close, modify or block anything. Entries, stops and exits behave exactly as before. It uses `fetch_*` calls only. Paper accounts are skipped. Nothing runs after a cycle exception.

## Alerts and records
* Reason code `foreign_order_or_position`, decision-log outcome `OBSERVED` when telemetry is on.
* The message is a fixed string listing symbol and kind. No ids, amounts or exchange text.
* Same finding set: one alert per 24 hours (`protection_guard.should_alert`). A new finding alerts again at once.
* A failed read sends no alert and logs one warning per hour.

## Switches
| Variable | Default | Meaning |
|---|---|---|
| `FOREIGN_ORDER_ALARM` | `off` | `off` or `alert`. Anything else is `off`. |
| `FOREIGN_ORDER_CHECK_MINUTES` | `15` | Minutes between looks per account. |

## Owner steps (the owner runs every VPS command, one at a time)
1. `python -m mba_bot.restart_gate` must exit 0 before any restart.
2. Deploy with the switch off. Check health and journal.
3. Set `FOREIGN_ORDER_ALARM=alert`, restart, watch one account for a day.
4. Rollback: remove the line (or set `off`) and restart. No data to undo.

## NOT verified
* Request-weight cost per account on real Binance. It is one open-orders read per pair, plus one positions read on Futures. Not measured.
* How Binance reports the client id on orders placed by its own web or app. Foreign orders are assumed to lack the `mba` prefix.
* Behaviour with more than a handful of manual pairs.
* Tests use the fake exchange. They prove control flow, not Binance behaviour.
