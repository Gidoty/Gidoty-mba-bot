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

## Cost per check (counted from the code; request weights are from memory of Binance's documentation, UNVERIFIED)
Reads run one after another at the end of the account's cycle, once per interval. Universe = the account pair, its manual pairs and its open trade pairs (4 manual pairs by default, 10 with Futures expansion).
* Spot: 1 `fetch_open_orders` per pair. About 4 to 5 calls per check by default. Weight about 6 per call (about 24 to 30 per check).
* Futures: 2 reads per pair (regular and conditional open orders) plus 1 `fetch_positions`. About 9 to 11 calls per check by default, about 21 to 23 with Futures expansion. Weight about 1 per order read and 5 for positions (about 13 to 15 by default).
At the 15-minute default that is a small share of Binance's per-minute limit, but this was not measured.

## NOT verified
* Request-weight cost per account on real Binance (counts above come from the code, the weights from memory). The reads are sequential, so a slow Binance response delays the end of that account's cycle by the sum of the call latencies. Not measured.
* The client-id prefix rule. An order placed in Binance's own app or web gets a generated client id; the alarm assumes it does not start with `mba`. A manual order that does start with `mba`, or a bot order with a changed id, would be misjudged. Not observed.
* Behaviour with more than a handful of manual pairs.
* Tests use the fake exchange. They prove control flow, not Binance behaviour.
