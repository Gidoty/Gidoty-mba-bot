# Give-back metric and owner alert

Status: built. The metric is always computed (reporting only). The alert is OFF by default. Depends on the peak-price PR (it reads `trades.peak_price`) and, for the shared report code, on the net-P&L PR.

## The metric (definitions the code uses)
R is the distance from the entry to the INITIAL stop. The best price is `trades.peak_price` (Part A). Both figures need the peak, the initial stop and the exit price on the trade, so older trades (no peak) are left out and the report says how many trades have one.
- Give-back rate: of the closed trades whose best price reached at least +1 R, the share that closed below the entry NET of costs (above it, for a short). "Net of costs" here is an ESTIMATE: the exit is compared with entry plus both fees at the assumed 5 bps and the assumed 11 bps round-trip slippage. It needs no fee data, so it also works for trades with no captured fees. A trade that closed a hair above the entry (less than the round-trip cost) counts as a give-back.
- Closed far below the peak: closed trades whose exit was more than 1 R worse than the best price (any trade, not only those that reached +1 R).
Shown in the admin results (per bot, mode and strategy, line "Give-back: ..." under the result) and in the trade report (`GET /api/reports/summary`: `trades_with_peak`, `reached_1r`, `gave_back_to_loss`, `give_back_rate`, `closed_far_below_peak`). The admin figures are computed in SQL and a test checks they equal the Python report on the same trades.

## The alert (`GIVEBACK_ALERT_FRACTION`, default 0 = OFF)
When an open LIVE trade has given back at least that fraction of the profit at its best price ((peak - price) / (peak - entry)), and its best price was at least `GIVEBACK_ALERT_MIN_PEAK_R` (default 1) x R in profit, the owner gets ONE Telegram message (`OWNER_ALERT_CHAT_ID`, falling back to `BILLING_ALERT_CHAT_ID`; log only if neither is set). Once per trade: the claim is stored in `alert_marks` (key `giveback:<trade id>`), so a restart or a second process does not repeat it. The text has the bot code, the pair, the side and numbers, never a name, an email or a key.
ALERT ONLY. It places no order, moves no stop and changes no state on the trade; a test checks that. Paper trades never alert. Clamped to 0.1 to 0.95; a typo means OFF.

## Limits
- The price used is the last candle's close from the round's candle read, so a fall and recovery between two rounds is not seen.
- The alert tells you a trade is giving profit back; it does not say what to do. That is the point of the staged lock, the time stop and Part H.

## The alert never raises into the cycle

The trade id is read before anything can fail, and the failure handler reads no ORM attribute. The claim (`protection_guard.claim_alert`, which commits once per trade) already falls back to its in-memory rule on any database trouble; a failure that still escapes it, or a failing owner send, is logged and swallowed. The handler rolls the session back only if its transaction cannot continue, so the cycle's other pending changes are kept. The log call and the rollback are each guarded.
