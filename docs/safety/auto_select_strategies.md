# Auto Select: every opted-in strategy is evaluated every round

Status: always on for the accounts it applies to; there is no new switch. It applies to an account only when ALL of these are true: the account uses Auto Select, it is a live (or demo) account, it is opted in to the strategy (`extra_strategies`, set by the operator), and the strategy and its live switch are on (`DONCHIAN_ENABLED` with `LIVE_DONCHIAN`, `RSI_MR_ENABLED` with `LIVE_RSI_MR`). Every other account runs exactly the code it ran before.

## What was wrong
On an Auto Select account `process_account` scanned the four Auto Select pairs for an EMA signal and returned at once when none had one. The Donchian and RSI entry checks sit after that point, so an account opted in to them never had them evaluated on rounds where EMA was quiet (most rounds). When EMA did signal, both ran on the single pair EMA had picked, not on the other pairs.

## What it does now
Per round, for each strategy the account is opted in to, in the order the code already used (Donchian first, then EMA, then RSI):
* The strategy is evaluated on every candidate pair of that round: the pairs the scan read, in the scan's order (BTC, ETH, BNB, XRP, minus pairs left out for small capital or a failed read). One code path: `_live_strategy_signal`, the same function the live multi-position path uses, applies the strategy's own closed-candle rule and its own entry window.
* If a pair has a fresh signal, the first such pair in scan order is entered through `_maybe_enter`, exactly as every other entry (kill switch, daily dollar limit, 50% total stop, guards, pre-entry protection check, leverage cap, risk per trade, protective stop placed and verified). The account's pair is set to that pair for the entry and put back if no trade opened. Other pairs that also had a signal are recorded in the ledger as `not_best_ranked`.
* With no EMA signal the same passes run before the round ends (Donchian, then RSI). With an EMA signal the order is unchanged: a Donchian hit on any candidate pair takes the one live slot first, otherwise EMA enters its pick, otherwise the RSI pass runs.

## Priority, as the code had it
`process_account` already said "highest priority: Donchian, then EMA, then RSI" and `_live_rsi_entry` only runs if no trade is open. That is unchanged. The code has NO rule for choosing between several pairs that signal for the same strategy (EMA ranks by its own score; Donchian and RSI have no score). This change uses the scan order; that is a choice to confirm, not something the code did before.

## What did not change
Entry and exit rules, risk per trade (the new strategies keep their own cap), the account risk cap, the leverage cap, protective stops, the protection pre-check, one live position per account, slot rules, EMA windows and the 10-minute entry window of the new strategies. Paper accounts, manual accounts and accounts not opted in never reach the new code (`_auto_new_strategies` returns an empty list; tests spy on it).

## Exchange reads
The EMA scan already reads each candidate pair's candles once per round (up to 4 reads, `OHLCV_LIMIT` candles each). Donchian and RSI reuse those candles when their timeframe is the scan's (the defaults are all 1h): ZERO extra reads. With a different timeframe a strategy adds one read per candidate pair for that strategy only, up to 4 for Donchian and 4 for RSI. For comparison, before the change a round where EMA found a signal made 2 extra single-pair reads (one each for Donchian and RSI, on the picked pair); with default timeframes those are now gone, and rounds where EMA found nothing add none. A failed read on one pair skips that pair for that strategy and logs one warning; the other pairs and the cycle go on.

## Opportunity ledger
Every evaluation writes its verdict row through `_live_strategy_signal` (`record_verdict`), so a check that now runs is recorded as evaluated. `_ledger_not_evaluated` stays for a strategy that could not be evaluated (all its reads failed), and never records one that ran.

## Rollback
No switch to flip. To stop an account using it, remove the strategy from its `extra_strategies` or switch the strategy's live switch off. Reverting the code restores the old behaviour.

## Not proven
The fake exchange and fixed candles stand in for Binance: nothing here shows any strategy is profitable.

## Measuring the tie-break (observability only; the pick is unchanged)
When two or more pairs signal for the same strategy in one round, the first pair in scan order is entered (BTC, ETH, BNB, XRP). That is an arbitrary rule, so its cost is measured, not guessed. Nothing below changes which pair is picked, and the trading code never imports it.
* **Ledger.** A Donchian or RSI signal row carries `strength` in its detail JSON: Donchian = how far the close is beyond the channel, in ATR units (long: above the channel high, short: below the channel low); RSI = how far the RSI is beyond its entry line, in RSI points (long: RSI minus the oversold line, short: the overbought line minus the RSI). The `not_best_ranked` row of a skipped pair carries `picked`, `picked_strength` and the skipped pair's own `strength`, and its `candle_ts` is the signal candle.
* **Report.** `python -m mba_bot.opportunity_report --hours 168` now starts with one line, `ties: X of Y rounds ...`: Y is the number of (account, strategy, signal candle) with at least one Donchian or RSI signal, X how many of them had two or more pairs signalling. Below the per-account part comes a `ties` section: for each tie (newest 20) the pair picked, the pairs skipped, every pair's strength, and, only with `--candles`, what the price did over the next 24 candles in the signal's direction as a fraction of the signal candle's close: best excursion, worst excursion and the move at the 24th candle. `--candles` makes one PUBLIC Binance candle read per pair (no key, no account, no order); without it the report makes no exchange call. When fewer than 24 candles have closed since the signal the line says `ONLY n OF 24 candles exist yet, PARTIAL` (or that none has closed yet).
* **What it is not.** A measurement of one fixed rule. It is not evidence that any ranking rule (strongest signal first, for example) works. The last sealed backtest window was already looked at once, so any rule must be judged on new forward data collected from now on. A tie needs `OPPORTUNITY_LEDGER` on and an Auto Select account opted in to the strategy; with the ledger off the section reads `0 of 0`.
