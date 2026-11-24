# Profit protection v2, Part F: net P&L (funding, fee asset, real fee tier, report lines)

Status: built. Nothing here changes a trade decision except the cost buffer's inputs. Base branch: `feature/profit-time-stop` (Part D), then C, B, A, and `feature/ema-exit-rules` (PR #84).

## 1. Futures funding per trade
- New nullable columns on `trades`: `funding_quote`, `funding_read_at`. Migration `a7d3e9f1c5b2` (not run by Claude; run it before the new code starts, `create_all` does not add columns to an existing table).
- `funding_quote` is the sum of the account's FUNDING_FEE income on the trade's symbol over the holding period, in USDT, signed from the account's side: negative = paid, positive = received. NULL = unknown (never read or the read failed). 0.0 = the read worked and found none. A Spot trade is written 0.0.
- Written at close (`lifecycle._finalize`). The read is wrapped: a failed or odd answer leaves NULL and the trade closes anyway. A test proves it.
- For the staged lock's cost buffer an OPEN trade's funding is re-read at most every 30 minutes, and only once stage 1 could act. A failed read uses what is stored.
- Endpoint and permission: ccxt `fetch_funding_history`, which on Binance USD-M is `GET /fapi/v1/income` with `incomeType=FUNDING_FEE` (UNVERIFIED against the live exchange). It is a read-only USER_DATA call: it needs the key's normal "Enable Reading" permission, no trading or withdrawal right and no change to the key (UNVERIFIED). No key permission change is asked for.
- Not captured: funding on trades closed before this code (NULL, no backfill).

## 2. A commission paid in BNB
`trades.fee_asset` and `fee_asset_amount` record the asset and the total amount paid, summed over the entry and exit fills read from the account's trade history (Futures). The quote-currency fee stays NULL: the bot does not value BNB (no price source is read at the fill), so such a trade has unknown fees and no net figure. One asset gives its name; more than one gives `MIXED` and no amount. Spot commissions in BNB are not recorded in this part (only the Futures trade-history path carries them); they stay NULL as before.

## 3. Reports and the dashboard
Gross, fees, funding and net are four separate figures (customer report table and the admin results API). `net_pnl = gross - fees + funding` and exists only for a live or demo group where EVERY closed trade has both fee numbers AND a known funding. A Spot trade counts as funding known (none exists); it is recognised by its protective-order kind (`stop_loss` or `stop_loss_limit`, against Futures `stop_market_*`) because the trade row does not store its market. Otherwise the wording is "after fees, before funding" or "before fees". Practice (paper) groups are simulated and never net. The daily Telegram summary uses the same words. A report built before this change shows funding as unknown for old Futures trades, so their net is not shown (it was before; that is the honest change).

## 4. The account's real fee tier
`fee_tier.taker_bps` reads the account's real taker rate (ccxt `fetch_trading_fee`; Futures `GET /fapi/v1/commissionRate`, Spot `GET /sapi/v1/asset/tradeFee`, both UNVERIFIED, read permission) at most once per 24 hours per account while a trade needs it, cached in memory only (nothing stored). A rate outside 0 to 1% is rejected. When it cannot be read (endpoint missing, error, odd value) the ASSUMED rate `FEE_GUARD_FEE_BPS` (5 bps, the standard USD-M taker fee) is used, and a failed read is not retried for an hour. It feeds:
- the staged lock's stage 1 buffer and the time stop's floor (Parts C and D), with the funding paid so far per unit;
- the EMA take profit (PR #84): the target is now entry plus (take-profit R x R) plus the cost buffer, so the target is net of costs. This changes PR #84's target by the buffer (about 0.21% of the entry at the defaults); with take profit off, nothing changes. Paper trades use the assumed rate.
It does not feed the fee guard at entry (that still uses `FEE_GUARD_FEE_BPS`).

## Limits
Nothing here has run against Binance. Funding timestamps and signs follow ccxt's unified shape; a different real shape would be rejected as unknown (None), never mis-signed silently into a net figure that looks known.
