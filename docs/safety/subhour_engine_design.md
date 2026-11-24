# Design note: what the engine needs before any sub-hour strategy can run live

Status: design only. No code, no switch, no live effect. Written because the owner asked for scalp and day-trade horizons.

## Read this first
The short-timeframe research (PR #85, plan hash d8f9a600f51048b6) found that no EMA, Donchian or RSI cell on 5m, 15m or 30m clears the pre-registered rule after costs. Net per trade on the tune window was between -0.16 R and -0.87 R, and the 1h control also failed (-0.030 R for EMA). Nothing here should be built until a strategy passes a sealed test. This note lists the work so you can judge the cost against that result.

Numbers marked MEASURED come from running one cycle against the repo's fake Binance client. Numbers marked UNVERIFIED are Binance facts I could not check from this environment.

## 1. Round cadence
- `run_poll_forever` runs a round, then the billing and renewal jobs, then sleeps `POLL_INTERVAL_SECONDS` (300). The real period is 300 s plus the round duration, so it drifts. At 20 accounts a round may take tens of seconds; the drift is small but real.
- `MAX_CONCURRENT_CYCLES` is 6 and `CYCLE_WATCHDOG_SECONDS` is 120. A cycle that exceeds 120 s is abandoned for that round.
- A 5m candle closes every 300 s. With a period of 300 s plus the round duration, the round will sometimes land after a candle closes and sometimes miss the boundary by a whole round, so a 5m signal can be seen up to about 10 minutes late. A 15m strategy has the same effect at a smaller scale.
- What it needs: rounds aligned to candle closes (sleep until the next boundary plus a small offset, not a fixed 300 s), and a separate cadence per timeframe. The recovery loop (every 15 s) already exists and only touches protection and unresolved trades.

## 2. EMA_ENTRY_WINDOW_MINUTES (and NEW_STRATEGY_ENTRY_WINDOW_MINUTES)
- Default 10 minutes, clamped 1 to 59. An entry is skipped as `signal_stale` if the signal candle closed more than the window ago (`trader._ema_entry_stale`).
- On 1h this means a signal is acted on only in the first 10 minutes of the next hour, which is about two rounds.
- On 5m the window is longer than the candle itself. A 10 minute window on a 5m strategy would accept a signal two full candles old. The window must become a fraction of the timeframe (for example one round period or 20% of the candle), not minutes.
- The clamp to 59 and the minutes unit both assume hourly candles. Both need to change.

## 3. Candle reads per round and Binance read weight
MEASURED, one cycle on the fake client (1h timeframe, Futures):

| Case | Calls per cycle |
|---|---|
| Manual pair, idle (no signal) | 2 (1 API-restriction check, 1 candle read) |
| Auto Select account, idle | 5 (1 restriction check, 4 candle reads, one per pair) |
| Open protected trade | 9 (restriction check, markets, position mode, multi-assets, positions, order lookup, 2 open-order reads, 1 candle read) |

Scaling, assuming every account is an Auto Select account scanning 4 pairs each round and every account runs its own reads:
- 20 customers: 20 x 5 = 100 calls per round idle, 20 x 9 = 180 with all holding a trade.
- 100 customers: 500 calls per round idle, 900 with all holding a trade.
- At a 300 s round, 100 customers is 100 calls per minute idle and 180 per minute with trades.

Rate limits. UNVERIFIED (I could not reach Binance docs from here): Binance USD-M Futures limits request weight per IP (reported as 2400 per minute) and orders per account. A klines call with limit 200 is reported as weight 2 and most account reads as 5 to 20 weight each. If those numbers hold:
- Candle reads are the same for every customer. Today each account fetches its own copy. All 100 customers reading BTC 1h candles is 100 identical calls. A shared candle cache per round removes that; it is the biggest saving available and is needed before any shorter timeframe.
- Account reads (positions, open orders) are per customer, and per-IP limits are shared by every customer if the VPS has one IP. 100 customers x 9 calls x about 10 weight is about 9000 weight per round, which is 1800 per minute at a 5 minute round. That is already close to a 2400 per minute ceiling, before any 5m cadence. At a 1 minute cadence it would be five times over.
- Binance reports weight used in response headers. The engine does not read them today. It needs to, and to back off on 429 and 418 responses.

## 4. Protective stop placement latency
- After entry the bot confirms the fill (`ENTRY_RESOLVE_ATTEMPTS` 5 at 1 s), then places the stop with `PROTECTION_MAX_ATTEMPTS` 3 at `PROTECTION_RETRY_DELAY_SECONDS` 2. Worst case before the position is flagged unprotected is roughly 5 s plus 6 s plus call time.
- If the stop fails, the 15 s recovery loop picks it up. So an unprotected window of up to about 30 s is possible in a bad case.
- On a 1h strategy with a 2 ATR stop, 30 s of exposure is small. On a 5m scalp with a stop 0.3 to 0.5% away, 30 s can be a sizeable share of the whole trade, and it costs a slippage hit on a worst-case exit.
- What it needs: measured placement latency per trade (store the time between fill and verified stop), and a rule such as "close at market if no stop within N seconds" tuned by timeframe. The bot has no such number stored now.

## 5. Restart behaviour with an open trade
- `run_forever` calls `recover_once(include_healthy=True)` before the first poll, so every live trade is reconciled against Binance. SQLite is not trusted after a restart. This is already built.
- Exchange-side stops keep protecting a position while the bot is down. Exits the bot decides itself (opposite crossover, and the monitored take profit and trailing stop in PR #84) do not happen while it is down.
- On a 1h strategy, a 10 minute outage delays an exit by at most one candle's worth of decision. On a 5m strategy the same outage skips two candles, so a signal exit or trail update can be missed entirely, and the trade can run on the old stop for much longer relative to its length.
- What it needs: exchange-side take profit orders (not monitored) for sub-hour trades, a heartbeat the owner sees, and a restart procedure that replays the candles missed during the outage before any new entry.

## 6. SQLite write load
- SQLite runs in WAL mode when `sqlite_wal` is set, with a 15 s busy timeout. There is one writer at a time.
- Per cycle the engine writes decisions (telemetry), opportunity ledger rows and trade state. I counted 9 session or commit sites in `trader.py` alone; a signal-less idle cycle still writes decision rows for each pair.
- Estimate (not measured): 100 customers x 4 pairs x 1 decision row = 400 inserts per round. At 5 minute rounds that is about 1.3 per second, which SQLite handles easily. At 1 minute rounds it is 6.7 per second plus trade writes, still within SQLite's range on an SSD but with a higher chance of `database is locked` waits across 6 concurrent cycles.
- Risk is not throughput but write contention: a long transaction in one cycle blocks the others up to 15 s, and the watchdog cancels at 120 s. On sub-hour rounds a few blocked cycles could skip a candle.
- What it needs: measure lock waits per round (not logged today), keep transactions short, and decide whether the decision log moves out of the main database before cadence drops below 5 minutes.

## Summary of what must exist before a sub-hour strategy goes live
1. A strategy that passes a sealed test. None does today.
2. Rounds aligned to candle closes, per timeframe.
3. Entry window expressed as a fraction of the timeframe.
4. A shared per-round candle cache, plus Binance weight tracking and backoff.
5. Measured stop placement latency and a hard rule when it is exceeded.
6. Exchange-side take profit, and a catch-up of missed candles after restart.
7. Lock-wait measurement for SQLite.

## Where I am unsure
- Binance request weights and the per-IP limit (UNVERIFIED).
- Whether all production customers share one IP.
- The SQLite write estimate (not measured on the real database).
- Round duration at 20 and 100 customers (I measured calls, not time).
