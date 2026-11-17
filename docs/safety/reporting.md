# Reporting (B2)

Status: on a branch (stacked on the multi-position PR for the migration chain; this PR has no migration), not merged, not deployed. Ships OFF (`REPORTING_V2`, default off). CLAUDE-RUN.

## Switch OFF
Identical to today: no report route (409), no report on the screen, and the daily Telegram summary is the old text (`pnl_labels.daily_summary`, which names the figure "net" when `PNL_LABELS` is off and "before fees" when it is on). A test pins the old wording.

## Switch ON
* **`GET /api/reports/summary[?window_days=N]`** (read-only): the logged-in customer's own account only. There is no account parameter (a test passes one and gets the customer's own data). Results per **mode** (live, demo, practice), then by **strategy**, then by **pair**. Live, demo and practice are never added together.
* **Gross** is the sum of `trades.pnl` (before exchange fees, before Futures funding). **Fees** are the sum of the real commissions captured from the fills (`entry_fee_quote` + `exit_fee_quote`). A group is `fees_complete` only if EVERY closed trade in it has both numbers (a captured fee of zero counts). **Net** (gross minus fees) is given only for a complete live or demo group; otherwise it is null and the wording is "before fees" with how many trades have fees. **Practice results are simulated**: no fees, no slippage, never net, whatever fee numbers a row has. Futures funding is never included in any figure (the report says so).
* **Daily Telegram summary:** one line per mode that closed trades that day. "net ... after fees" only if every trade of that mode that closed that day has both real fees; otherwise "before fees" (and "fees known for k of n"); practice "simulated, no fees or slippage"; demo says "no real money".
* **Screen:** the Performance page shows a "Results by strategy and pair" card (only when the service offers it). The screen never computes a net itself; it shows what the server sent.
* **No secrets:** the response holds numbers, counts and labels only (a test scans for key, secret, password, e-mail, token and account id).

## Where the fees come from
Fee capture (merged): Spot entry fee from the order response, Futures entry and exit fees read at close, exit fees summed over the fills. Old trades and trades whose fee could not be valued exactly (for example a fee paid in BNB) have no fee numbers and are reported "before fees". There is no backfill.

## No migration. Owner steps and rollback
`REPORTING_V2=true`, restart gate, restart. Rollback: remove the line and restart.

## NOT verified
Real Binance fees on real fills (the fake exchange sets its own); that the captured fee columns are populated on the owner's real trades (only trades closed after the fee-capture deploy have them); the screen in a browser (a build and a source test only). Funding payments are not captured at all.
