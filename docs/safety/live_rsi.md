# Live RSI mean reversion (B5a)

Status: on a branch (stacked on the demo runner PR), not merged, not deployed. Ships OFF (`LIVE_RSI_MR`, default off). CLAUDE-RUN against a FAKE exchange; no profit is claimed and the strategy is unproven.

## Switch OFF
Identical to today: `rsi_mr_v1` is paper only, a live account never runs the RSI code (a test poisons it).

## Switch ON, what changes
An account that is **opted in** to `rsi_mr_v1` (the owner-set `extra_strategies` column, as for paper) and is LIVE (or DEMO), with `RSI_MR_ENABLED=true`, `NEW_STRATEGY_GUARDS_MODE=enforce` and `FEE_GUARD_MARGIN` above 0, may take an RSI entry:
* Signal from CLOSED candles only on `RSI_MR_TIMEFRAME` (rules in `rsi_mr_v1.md`), the account's own pair, long on Spot, long or short on Futures; the signal candle must be the latest closed one and have closed within `NEW_STRATEGY_ENTRY_WINDOW_MINUTES`.
* **One live slot, EMA first:** RSI is tried only if EMA took no entry this cycle and no trade is open. (The priority across all strategies, Donchian, EMA, RSI, is in the multi-position PR.)
* The entry goes through the same `_maybe_enter` and `lifecycle.open_live_trade` as an EMA entry: kill switch (early and before the order), daily dollar limit and 50% total stop, strategy guards in enforce and the fee guard, the pre-entry protection check, leverage cap and size_cap, risk per trade (the smaller of the account's and `RSI_MR_RISK_PCT`, default 0.5), stop 1.5 ATR, the protective stop placed and verified right after the fill, emergency exit if it cannot be.
* The trade is recorded `strategy_id = rsi_mr_v1`; Telegram says "Strategy: rsi_mr_v1".
* **Exits by its own rules:** profit target (1 ATR), RSI back to 50, time stop (24 candles), judged on the closed candles after the entry, through `lifecycle.execute_exit` (the only live exit path). The stop is the exchange-side stop and the software backstop; a stop touch found in the candles is left to them. An EMA crossover never closes an RSI trade. An open RSI trade is managed even if the switch is turned off.

## Rollback
Remove `LIVE_RSI_MR` and restart (restart gate first). An RSI trade that is open keeps being managed. No migration.

## NOT verified
* Anything about profit. The 2 wins in 27 paper trades were decided on testnet/demo candles (see `docs/ops/paper_source_and_replay.md`); the replay against mainnet data is the owner's step before this goes on.
* Binance behaviour; the fake exchange stands in.
* Live fills, slippage and fees against the strategy's 1.0 ATR target (it is small relative to costs; the fee guard exists for this).

## Auto Select
An Auto Select account opted in to this strategy now has it evaluated every round on every candidate pair, whether or not EMA found a signal: see `docs/safety/auto_select_strategies.md`.
