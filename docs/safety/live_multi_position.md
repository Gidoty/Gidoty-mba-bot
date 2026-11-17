# Live multi-position (B7)

Status: on a branch (stacked on the live Donchian PR), not merged, not deployed. Ships OFF (`LIVE_MULTI_POSITION`, default off). CLAUDE-RUN against a FAKE exchange (one fake per pair).

## Switch OFF
Identical to today: a live account holds one non-terminal trade, whatever its `live_slots` says (a test sets 3 slots with the switch off and sees one trade). The recovery path for an unexpected second live trade is unchanged.

## Switch ON
* **Slots:** each account has its own `live_slots` (1 to 3, empty = 1), set by the customer on the Trading tab of Bot Settings (only shown when the service offers it). `live_multi.slot_cap` clamps it; the hard maximum is 3.
* **One per pair, one entry per cycle:** the pairs are the account's pair plus its owner-set `pair_list` (the same list paper multi-trade uses). Each cycle every open live trade is reconciled and managed with ITS strategy's rules (EMA opposite crossover; RSI target, RSI line, time stop; Donchian channel exit and trailing stop). Then at most ONE new entry, if a slot is free, on a pair with no open trade, in the priority **Donchian, then EMA, then RSI**; a strategy that is refused falls through to the next.
* **Every entry** goes through the same `_maybe_enter` and `lifecycle.open_live_trade` as a single-slot entry: kill switch, the daily dollar limit and the 50% total stop (both are account equity measures and already cover all trades and strategies together), strategy guards (the portfolio guard now counts the account's slots), pre-entry protection check, leverage cap, risk per trade, protective stop placed and verified.
* **Shared capital:** a second or third trade is sized from the trading capital the open trades have not already used (position value / leverage cap on Futures, / 1 on Spot). No capital left: refused (`live_free_capital_short`).
* **Open-risk cap (PROPOSAL, owner to decide):** for a second or third trade the sum of the open risk of every open trade at its CURRENT stop (quantity x distance to the stop, so a trailed stop lowers it) plus the new trade's risk must be within (a) `LIVE_TOTAL_RISK_CAP_PCT` percent of the trading capital (default 3, 0 = off) and (b) the customer's daily dollar limit (when `ACCOUNT_DAILY_LOSS_LIMIT` is on), so that all stops hit together cannot lose more than the daily limit allows. Refusals: `live_total_open_risk_cap`, `live_open_risk_above_daily_limit`. The first trade is judged by the existing rules only.
* **Auto Select** trades one pair at a time: the API refuses more than one slot while Auto Select is on, and Auto Select while more than one slot is set. Slots do not change the pair of an open trade.
* Lowering `live_slots` never closes a trade; it only stops new entries until fewer are open.

## Database rule
The audit-B1 index "one non-terminal live trade per account" would forbid a second slot. Migration `d9b5f7c2e4a6` replaces it with the same rule per slot: `trades.live_slot` (every live trade has one; existing live rows backfilled to 0; a live row without one takes 0) and the unique index `uq_trades_live_slot (account_id, live_slot)` over non-terminal live rows. With the switch off every live trade takes slot 0, which is the old rule. The upgrade and the downgrade both REFUSE, changing nothing, if an account holds more than one non-terminal live trade.

## Migrations
`c8a4e6b1d3f5` (`trading_accounts.live_slots`, nullable int) then `d9b5f7c2e4a6` (above). Additive in effect, reversible (see the refusal rule), not run.

## Owner steps and rollback
Backup, restart gate (exit 0 means no live trade is open, which also satisfies the migration), `alembic upgrade head`, deploy, restart; set the pair list for the account (`pair_list`), then `LIVE_MULTI_POSITION=true` and the customer sets the slots. Rollback: set `live_slots` back to 1 or remove the switch and restart; open trades keep being managed one by one (the existing recovery path). `alembic downgrade b7f3d5a9c2e4` needs at most one open live trade per account.

## NOT verified
Binance's shared-margin behaviour across several Futures positions (each fake has its own balance); the liquidation interaction of cross positions (isolated margin only is allowed, per pair); the effect of several simultaneous stops on rate limits; profit.
