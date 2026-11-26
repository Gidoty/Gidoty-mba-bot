# Profit protection v2, Part A: peak tracking (best price since entry)

Status: built. Based on `main` (no other PR needed). Switch: none (it only records a number). Live effect: none on any order.

## What it does
Every trade, live and paper, any strategy, now carries `trades.peak_price`: the best price reached since the entry. For a long that is the highest price seen, for a short the lowest. It is refreshed every round (5 minutes) from the live price and from the highs and lows of the candles that started after the entry.

Rules the code enforces (each has a test):
1. It only moves in the favourable direction. A pullback never lowers a long's peak.
2. A candle that started before the entry is never counted (its extreme may predate the trade). The still-forming candle that started after the entry is counted.
3. It is never worse than the entry price.
4. A failure is logged and never stops trade management, an exit or a stop move. The write makes no commit of its own: the peak is a pending change that joins the cycle's transaction (it is saved with the cycle's next commit). The handler reads no attribute from the trade (the id is read before anything can fail) and rolls the session back only if its transaction cannot continue.
5. Nothing in the entry path reads it. A test inspects the source of `_maybe_enter`, `_ema_entry_stale`, `_live_strategy_signal` and the RSI and Donchian entry functions.

## Migration (not run by Claude)
`d4a1b7c3e9f2_trade_peak_price`: one nullable `Float` column on `trades`, no default, no backfill. Idempotent and reversible (the downgrade drops the column and the peaks with it). `create_all()` does not add a column to an existing table, so the migration must run before the new code starts, the same restart-gate order as earlier columns.

An old trade that is open when the code arrives has `peak_price` NULL. At its first managed round the peak is set from the entry price, the live price and the candles still returned by the exchange (up to 200 of the strategy's candles) that started after the entry. For a trade that has run longer than the candle window, or one whose best price fell between two rounds, the stored peak is a lower bound, not the true best price. A closed trade stays NULL.

## Where it shows
`GET /api/admin/open-trades` (admin only, behind the existing admin sign-in) lists open live trades by bot code (no names, no emails) with entry, initial stop, resting stop, peak and the peak in R (`(peak - entry) / |entry - initial stop|`). Part G uses the same number for the give-back metric.

## Limits
- Between two rounds the price can go higher and come back. The 1h candle highs cover most of that, but a spike inside a still-forming candle that retraced before the next round is seen through the candle high only if the exchange includes it in the forming row (it does for Binance klines, UNVERIFIED here).
- It measures. It is not evidence that any rule built on it makes money.
