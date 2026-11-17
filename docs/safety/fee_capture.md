# Real fee capture

Status: on a branch (PR 2), not merged, not deployed. Records data only.

## What changed
`trades` gets two nullable float columns, `entry_fee_quote` and `exit_fee_quote`: the actual commission in the quote currency (USDT), summed over the fills. Written only by `lifecycle`. `entry_fee_base` (Spot, base asset) already existed and is unchanged.

Conversion (`venue.fee_quote_of`): a quote-currency fee is added as is; a base-currency fee is multiplied by the fill price; any other asset (for example BNB), an unreadable or non-finite cost, or no fee entries gives NULL. A fee is never guessed.
Futures: an order response carries no fee, so the fees are read from the account's trade records (`fetch_my_trades`) ONLY when the trade is closed, in `lifecycle._finalize`, for the entry and the exit together. No trade-history read happens in the entry path (so none sits between the fill and the protective stop) or on any exit path before the close (`_exit_fills` never reads fees). A failing or slow read at close leaves NULL and cannot change what the lifecycle does: the trade is already closed. A Futures trade therefore has `entry_fee_quote` NULL while it is open. A test fails if any `fetch_my_trades` call happens before the protective stop.
Paper trades stay NULL. P&L is unchanged: nothing in the trading path reads these columns (a test checks the source).

## Migration `d2a5b9c7e1f4`
Parent `c1f4a8d2e6b3`. Adds the two columns. Idempotent (skips existing columns), reversible (`downgrade` drops them). Additive only.
Owner steps: back up the database, deploy, `alembic upgrade head`, check `alembic current` shows `d2a5b9c7e1f4`. Rollback: `alembic downgrade c1f4a8d2e6b3`. The owner runs these; nothing here ran them.

## Where it is shown
`pilot_status` and the paper export do not need it; use the columns directly with read-only SQL.

## NOT verified
* What Binance returns in `fee` for each market on real fills (BNB discount, VIP tiers, partial fills). Tests use ccxt-shaped fakes.
* Funding payments are not fees and are not captured.
* Existing closed trades stay NULL; there is no backfill.
