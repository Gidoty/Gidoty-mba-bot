# EMA decides on closed candles only (`EMA_CLOSED_CANDLE_ONLY`)

Status: shipped OFF. With the switch off, every EMA decision is made exactly as before this change (a golden test pins that). Nothing here runs until the owner sets the switch.

## The problem

`trader.py` passed `candles_from_ohlcv(ohlcv)` straight into `generate_signal`. Binance returns the candle that is still forming as the last row of `fetch_ohlcv`. So on an hourly timeframe, polled every 5 minutes:

* a crossover, the ATR flat-market filter and the opposite-crossover EXIT were decided on a partial candle, and could appear and vanish before the candle closed;
* the same partial crossover was seen on up to 12 polls of that hour.

`rsi_mr_v1` and `donchian_breakout_v1` already read closed candles only (`rsi_mr.closed_only`). The EMA paper record at the time of writing is 2 wins in 27 trades; the cause has not been established and this switch is one candidate (OWNER-REPORTED figure, not re-checked here).

## What the switch does

`EMA_CLOSED_CANDLE_ONLY=true` (default `false`):

1. Every EMA call site drops candles whose open time plus the timeframe is after `now`, through one helper (`trader._ema_signal_candles`) and one `now` reading per cycle:
   * the live open-trade monitor (crossover exit), `trader.process_account`;
   * the single-trade entry scan and the paper exit check;
   * Auto Select (`_scan_auto_pair`: ranking, flat filter telemetry and the final signal);
   * the multi-trade paper cycle (`_process_multi`: per-pair cache, exits, `last_signal`, entry scan);
   * the multi-live recovery monitor (`_process_multi_live`).
2. Prices do NOT come from the closed list. Sizing, stop and the market order use the live last row (`candles[-1].close`), as before. Only the signal waits for a close. Using the closed candle's close would size and stop from a price up to an hour old.
3. Telemetry rows (`candle_ts`) are stamped with the closed signal candle, so the one-decision-per-candle de-duplication in the decision log still holds across the 5-minute polls.
4. An unsupported `TIMEFRAME` (a week, a capital-M month, a typo) with the switch on gives no candles: no signal, no entry, no crossover exit. One warning per day. Exchange-side stops still protect an open trade.

## Stale-entry guard: `EMA_ENTRY_WINDOW_MINUTES` (default 10, clamped 1 to 59)

With closed candles only, a crossover on the last closed candle stays visible to every poll for the next hour. Without a guard the bot could enter 55 minutes late at a worse price.

With the switch on, an EMA entry is taken only if the signal candle closed at most `EMA_ENTRY_WINDOW_MINUTES` ago. Otherwise the entry is skipped, logged once per signal candle as `signal_stale` (decision log outcome `skipped`, strategy `ema_cross_atr_v1`, with `age_minutes` and `window_minutes`), and no order is sent.

Why a separate EMA setting and not `NEW_STRATEGY_ENTRY_WINDOW_MINUTES`: that one belongs to two paper-only strategies. Sharing it would let an edit made for them change what a live EMA customer's bot does. The default of 10 minutes is the same value; it covers the first two polls after a close (at +0 and +5 minutes, +10 inclusive) and tolerates a late poll. It is provisional and untuned.

The window applies to ENTRIES only. A crossover EXIT is never window-limited: an open trade exits on the first poll after the closing candle shows the opposite cross, however late.

Bearish crossovers on Spot (never entered) are not logged as stale.

## Owner steps (the owner runs every one; nothing here is done by the code)

1. Check no live trade is open: `python -m mba_bot.restart_gate` from the app directory with the service's `DATABASE_URL`. Exit 0 = none. Exit 3 = a live trade is open: do NOT restart. Exit 2 = unreadable: do not restart.
2. Only after exit 0: add the line `EMA_CLOSED_CANDLE_ONLY=true` to the server environment by hand (and optionally `EMA_ENTRY_WINDOW_MINUTES=10`). Restart the service. Check health and the journal.
3. Never restart with a live trade open. The setting is read at start-up.
4. Watch the decision log: `signal_stale` rows should appear only for crossovers seen late. Expect fewer entries than before, and each one up to one candle later than a forming-candle entry, because partial crossovers no longer count.
5. Rollback: remove the line (or set it to `false`), run the restart gate again, restart. Behaviour returns to today's. No database change is involved, so nothing to downgrade.

## Tests (CLAUDE-RUN, fake exchange, scratch SQLite)

`tests/test_ema_closed_candle.py` replays 22 hourly steps through the real `process_account` with a frozen clock, the last candle always forming:

* golden: with the default, signals, trades and decision-log rows equal the literals recorded on unchanged main;
* a crossover that exists only on the forming candle gives a trade with the switch off and none with it on; the same crossover is taken once its candle has closed, at the live price;
* the crossover exit likewise (single trade, multi-trade, Auto Select, live monitor, multi-live recovery monitor);
* a stale entry is skipped, logged once per candle (rows and log lines), keyed on the closed candle;
* window edge, unsupported timeframe, defaults off in code and in the environment.

## NOT verified

* That Binance returns the forming candle as the last row of `fetch_ohlcv`: not tested against the live exchange (no network was used). The code relies on the ccxt shape and on the candle timestamps being open times.
* That the exchange clock and the server clock agree. A server clock that runs ahead treats a forming candle as closed; one that runs behind delays entries (and can make a fresh signal look not-yet-closed). Not measured.
* That closed-candle EMA is better. No backtest or paper result for the switch exists; the effect on win rate, trade count and slippage is UNKNOWN.
* That 10 minutes is the right window. Untuned.
* Only the `1h` timeframe was replayed end to end. Other sizes were checked only through the helper's shared timeframe parser.
* The fake exchange proves control flow only; it says nothing about Binance.
